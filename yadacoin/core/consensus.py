import sys
from sys import exc_info
from os import path
import json
import logging
import datetime
from bitcoin.wallet import P2PKHBitcoinAddress
from time import time
from urllib3.exceptions import *
from asyncio import sleep as async_sleep
from pymongo.errors import DuplicateKeyError
from tornado.httpclient import AsyncHTTPClient, HTTPRequest
from tornado.httputil import HTTPHeaders
from tornado import ioloop
from yadacoin.core.chain import CHAIN
from yadacoin.core.config import get_config
from yadacoin.core.blockchain import Blockchain
from yadacoin.core.block import Block, BlockFactory
from yadacoin.core.transaction import (
    InvalidTransactionException,
    InvalidTransactionSignatureException,
    MissingInputTransactionException,
    NotEnoughMoneyException
)
from yadacoin.core.latestblock import LatestBlock
from yadacoin.socket.node import NodeSocketServer


class BadPeerException(Exception):
    pass


class AboveTargetException(Exception):
    pass


class ForkException(Exception):
    pass


class Consensus(object):

    lowest = CHAIN.MAX_TARGET

    def __init__(self, debug=False, prevent_genesis=False):
        self.app_log = logging.getLogger("tornado.application")
        self.debug = debug
        self.config = get_config()
        self.mongo = self.config.mongo
        self.prevent_genesis = prevent_genesis
        self.latest_block = None

    def output(self, string):
        sys.stdout.write(string)  # write the next character
        sys.stdout.flush()                # flush stdout buffer (actual character display)
        sys.stdout.write(''.join(['\b' for i in range(len(string))])) # erase the last written char

    def log(self, message):
        # TODO: deprecate, use app_log
        print(message)

    async def insert_genesis(self):
        #insert genesis if it doesn't exist
        genesis_block = await BlockFactory.get_genesis_block()
        await genesis_block.save()
        self.mongo.db.consensus.update({
            'block': genesis_block.to_dict(),
            'peer': 'me',
            'id': genesis_block.signature,
            'index': 0
        },
        {
            'block': genesis_block.to_dict(),
            'peer': 'me',
            'id': genesis_block.signature,
            'index': 0
        },
        upsert=True)
        self.latest_block = genesis_block

    async def verify_existing_blockchain(self, reset=False):
        self.app_log.info('verifying existing blockchain')
        existing_blockchain = await Blockchain.init_async(self.config.mongo.async_db.blocks.find({}).sort([('index', 1)]))
        result = await existing_blockchain.verify()
        if result['verified']:
            print('Block height: %s | time: %s' % (self.latest_block.index, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            return True
        else:
            self.app_log.critical(result)
            if reset:
                if 'last_good_block' in result:
                    self.mongo.db.blocks.remove({"index": {"$gt": result['last_good_block'].index}}, multi=True)
                else:
                    self.mongo.db.blocks.remove({"index": {"$gt": 0}}, multi=True)
                self.app_log.debug("{} {}".format(result['message'], '...truncating'))
            else:
                self.app_log.critical("{} - reset False, not truncating - DID NOT VERIFY".format(result['message']))
            self.config.BU.latest_block = None
            latest_block = self.config.LatestBlock.block
            if latest_block:
                self.latest_block = await Block.from_dict(latest_block)
            else:
                if not self.prevent_genesis:
                    await self.insert_genesis()

    def remove_pending_transactions_now_in_chain(self, block):
        #remove transactions from miner_transactions collection in the blockchain
        self.mongo.db.miner_transactions.remove({'id': {'$in': [x['id'] for x in block['block']['transactions']]}}, {'_id': 0})

    def remove_fastgraph_transactions_now_in_chain(self, block):
        self.mongo.db.fastgraph_transactions.remove({'id': {'$in': [x['id'] for x in block['block']['transactions']]}}, {'_id': 0})

    def get_consensus_blocks_by_index(self, index):
        return self.mongo.db.consensus.find({'index': index, 'block.prevHash': {'$ne': ''}, 'block.version': CHAIN.get_version_for_height(index)}, {'_id': 0})

    def get_consensus_block_by_index(self, index):
        return self.get_consensus_blocks_by_index(index).limit(1)[0]

    async def get_next_consensus_block_from_local(self, block):
        #table cleanup
        new_block = await self.mongo.async_db.consensus.find_one({
            'block.prevHash': block.hash,
            'block.index': (block.index + 1),
            'block.version': CHAIN.get_version_for_height((block.index + 1))
        })
        if new_block:
            new_block = await Block.from_dict( new_block['block'])
            if int(new_block.version) == CHAIN.get_version_for_height(new_block.index):
                return new_block
            else:
                return None
        return None

    async def get_previous_consensus_block_from_local(self, block, peer):
        #table cleanup
        new_block = await self.mongo.async_db.consensus.find_one({
            'block.hash': block.prev_hash,
            'block.index': (block.index - 1),
            'block.version': CHAIN.get_version_for_height((block.index - 1)),
            'ignore': {'$ne': True}
        })
        if new_block:
            new_block = await Block.from_dict(new_block['block'])
            if int(new_block.version) == CHAIN.get_version_for_height(new_block.index):
                return new_block
            else:
                return None
        return None

    async def get_previous_consensus_block_from_remote(self, block, peer):
        # TODO: async conversion
        retry = 0
        while True:
            try:
                url = 'http://' + peer.to_string() + '/get-block?hash=' + block.prev_hash
                if self.debug:
                    print('getting block', url)
                res = requests.get(url, timeout=1, headers={'Connection':'close'})
            except:
                if retry == 1:
                    raise BadPeerException()
                else:
                    retry += 1
                    continue
            try:
                if self.debug:
                    print('response code: ', res.status_code)
                new_block = await Block.from_dict(json.loads(res.content.decode('utf-8')))
                if int(new_block.version) == CHAIN.get_version_for_height(new_block.index):
                    return new_block
                else:
                    return None
            except:
                return None

    async def insert_consensus_block(self, block, peer):
        await self.mongo.async_db.consensus.replace_one({
            'id': block.to_dict().get('id'),
            'peer': peer.to_dict()
        },
        {
            'block': block.to_dict(),
            'index': block.to_dict().get('index'),
            'id': block.to_dict().get('id'),
            'peer': peer.to_dict()
        }, upsert=True)

    async def sync_bottom_up(self):
        if not self.latest_block:
            self.latest_block = LatestBlock.block

        if LatestBlock.block.index > self.latest_block.index:
            self.app_log.info('Block height: %s | time: %s' % (self.latest_block.index, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

        self.latest_block = LatestBlock.block
        latest_consensus = await self.mongo.async_db.consensus.find_one({
            'index': self.latest_block.index + 1,
            'block.version': CHAIN.get_version_for_height(self.latest_block.index + 1),
            'ignore': {'$ne': True}
        })
        if latest_consensus:
            self.remove_pending_transactions_now_in_chain(latest_consensus)
            self.remove_fastgraph_transactions_now_in_chain(latest_consensus)
            latest_consensus = await Block.from_dict( latest_consensus['block'])
            if self.debug:
                self.app_log.info("Latest consensus_block {}".format(latest_consensus.index))

            records = await self.mongo.async_db.consensus.find({
                'index': self.latest_block.index + 1,
                'block.version': CHAIN.get_version_for_height(self.latest_block.index + 1),
                'ignore': {'$ne': True}
            }).to_list(length=100)
            for record in sorted(records, key=lambda x: int(x['block']['target'], 16)):
                await self.import_block(record)

            last_latest = self.latest_block
            self.latest_block = await Block.from_dict(await self.config.BU.get_latest_block_async())
            if self.latest_block.index > last_latest.index:
                self.app_log.info('Block height: %s | time: %s' % (self.latest_block.index, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            latest_consensus_now = await self.mongo.async_db.consensus.find_one({
                'index': self.latest_block.index + 1,
                'block.version': CHAIN.get_version_for_height(self.latest_block.index + 1),
                'ignore': {'$ne': True}
            })

            if latest_consensus_now and latest_consensus.index == latest_consensus_now['index']:
                return False
            elif latest_consensus_now and  latest_consensus.index < latest_consensus_now['index']:
                return True
            return False
        else:
            return False
    
    async def request_blocks(self, peer_string):
        self.app_log.debug('requesting {} from {}'.format(self.latest_block.index + 1, peer_string))
        try:
            url = 'http://{peer}/get-blocks?start_index={start_index}&end_index={end_index}'\
                .format(peer=peer_string,
                        start_index=int(self.latest_block.index) +1,
                        end_index=int(self.latest_block.index) + 100)
            h = HTTPHeaders({"Connection": "close"})
            request = HTTPRequest(url, headers=h, connect_timeout=3,request_timeout=5)
            response = await self.config.http_client.fetch(request)
            if response.code != 200:
                return
            # result = requests.get(url, timeout=2)
        except HTTPError as e:
            self.app_log.warning('Error requesting from {} ...'.format(peer_string))
            # add to failed peers
            await self.peers.increment_failed(peer)
            return
        except ConnectTimeoutError as e:
            self.app_log.warning('Timeout requesting from {} ...'.format(peer_string))
            # add to failed peers
            await self.peers.increment_failed(peer)
            return
        except Exception as e:
            self.app_log.error('error {} requesting from {} ...'.format(e, peer_string))
            await self.peers.increment_failed(peer)
            return
        try:
            blocks = json.loads(response.body.decode('utf-8'))
            if not isinstance(blocks, list):
                raise ValueError("wrong get-blocks response, probably not whitelisted")
            # blocks = json.loads(result.content)
        except ValueError:
            return
        inserted = False
        for block in blocks:
            block = await Block.from_dict(block)
            latest_block = await self.config.BU.get_latest_block_async()
            if block.index == (latest_block['index'] + 1):
                await self.insert_consensus_block(block, peer)
                # print("consensus ok", block.index)
                res = await self.import_block({'peer': peer_string, 'block': block.to_dict(),
                                                'extra_blocks': blocks},
                                                trigger_event=False)
                # print("import ", block.index, res)
                if res:
                    self.latest_block = block
                    inserted = True
                else:
                    # 2 cases: bad block, or retrace.
                    if latest_block['index'] == self.latest_block.index:
                        # bad block, nothing moved, early exit
                        self.app_log.debug('Bad block {}'.format(block.index))
                    else:
                        # retraced, sync
                        self.latest_block = await Block.from_dict(latest_block)
                        self.app_log.debug('retraced up to {}'.format(self.latest_block.index))
                        inserted = True
                    # in both case, no need to process further blocks
                    break
            else:
                break
                #print("pass", block.index)
        if inserted:
            await self.trigger_update_event()
            # await self.peers.on_block_insert(self.latest_block.to_dict())

    async def trigger_update_event(self, block: dict=None):
        """Update BU latest block info if unknown, then trigger the event to all connected peers"""
        if block is None:
            block = await self.config.BU.get_latest_block_async()
        await self.peers.on_block_insert(block)  # This will propagate to everyone

    async def import_block(self, block_data: dict, trigger_event=True) -> bool:
        """Block_data contains peer and block keys. Tries to import that block, retrace if necessary
        sends True if that block was inserted, False if it fails or if a retrace was needed.

        This is the central entry point for inserting a block, that will modify the local chain and trigger the event,
        unless we asked not to, because we're in a batch insert context"""
        try:
            block = await Block.from_dict(block_data['block'])
            peer = Peer.from_string(block_data['peer'])
            if 'extra_blocks' in block_data:
                extra_blocks = None
                # extra_blocks = [await Block.from_dict( x) for x in block_data['extra_blocks']]  # Not used later on, just ram and resources usage
            else:
                extra_blocks = None
            self.app_log.debug("Latest block was {} {} {} {}".format(self.latest_block.hash, block.prev_hash, self.latest_block.index, (block.index - 1)))
            if int(block.index) > CHAIN.CHECK_TIME_FROM and int(block.time) < int(self.latest_block.time):
                self.app_log.warning("New block {} can't be at a sooner time than previous one. Rejecting".format(block.index))
                await self.mongo.async_db.consensus.update_one(
                    {
                        'peer': peer.to_string(),
                        'index': block.index,
                        'id': block.signature
                    },
                    {'$set': {'ignore': True}}
                )
                await self.retrace(block, peer)
                if trigger_event:
                    await self.trigger_update_event()
                return False
            if int(block.index) > CHAIN.CHECK_TIME_FROM and (int(block.time) < (int(self.latest_block.time) + 600)) and block.special_min:
                self.app_log.warning("New special min block {} too soon. Rejecting".format(block.index))
                await self.mongo.async_db.consensus.update_one(
                    {
                        'peer': peer.to_string(),
                        'index': block.index,
                        'id': block.signature
                    },
                    {'$set': {'ignore': True}}
                )
                return False
            fork_exception = False
            try:
                result = await self.integrate_block_with_existing_chain(block, extra_blocks)
                if result is False:
                    # TODO: factorize
                    await self.mongo.async_db.consensus.update_one(
                        {
                            'peer': peer.to_string(),
                            'index': block.index,
                            'id': block.signature
                        },
                        {'$set': {'ignore': True}}
                    )
                elif trigger_event:
                    await self.trigger_update_event(block_data['block'])
                return result
            except DuplicateKeyError as e:
                await self.mongo.async_db.consensus.update_one(
                    {
                        'peer': peer.to_string(),
                        'index': block.index,
                        'id': block.signature
                    },
                    {'$set': {'ignore': True}}
                )
            except AboveTargetException as e:
                await self.mongo.async_db.consensus.update_one(
                    {
                        'peer': peer.to_string(),
                        'index': block.index,
                        'id': block.signature
                    },
                    {'$set': {'ignore': True}}
                )
            except ForkException as e:
                fork_exception = True
            except IndexError as e:
                await self.retrace(block, peer)
                if trigger_event:
                    await self.trigger_update_event()
                return False
            except Exception as e:
                from traceback import format_exc
                self.app_log.warning(format_exc())
                await self.mongo.async_db.consensus.update_one(
                    {
                        'peer': peer.to_string(),
                        'index': block.index,
                        'id': block.signature
                    },
                    {'$set': {'ignore': True}}
                )
        except Exception as e:
            from traceback import format_exc

            exc_type, exc_obj, exc_tb = exc_info()
            fname = path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            self.app_log.warning(format_exc())
            if trigger_event:
                await self.trigger_update_event()
            return False

        if fork_exception:
            await self.retrace(block, peer)
            if trigger_event:
                await self.trigger_update_event()
            return False

        if trigger_event:
            await self.trigger_update_event()
        return True

    async def process_next_block(self, block_data: Block) -> bool:
        """This is the common entry point for all new possible blocks to enter consensus and chain"""
        block_object = await Block.from_dict(block_data)
        if block_object.in_the_future():
            # Most important
            self.app_log.warning('Block in the future for height %s  from peer: %s' % (block_object.index, peer.to_string()))
            return False
        # TODO: there is no check before inserting into consensus (previous hash, nor diff, nor valid tx)
        await self.insert_consensus_block(block_object, peer)
        self.app_log.debug("Consensus ok {}".format(block_object.index))
        return True

    async def integrate_block_with_existing_chain(self, block: Block, extra_blocks=None):
        """Even in case of retrace, this is the only place where we insert a new block into the block collection and update BU"""
        self.app_log.warning('integrate_block_with_existing_chain')
        try:
            # TODO: reorg the checks, to have the faster ones first.
            # Like, here we begin with checking every tx one by one, when <e did not even check index and provided hash matched previous one.
            try:
                block.verify()
            except Exception as e:
                self.app_log.warning("Integrate block error 1: {}".format(e))
                return False

            await self.config.mongo.async_db.blocks.delete_many({'index': {'$gte': block.index}})
            self.latest_block = await Block.from_dict(await self.config.BU.get_latest_block_async(False))

            async def get_txns(txns):
                for x in txns:
                    yield x

            async def get_inputs(inputs):
                for x in inputs:
                    yield x

            used_inputs = {}
            i = 0
            async for transaction in get_txns(block.transactions):
                self.app_log.warning('verifying txn: {} block: {}'.format(i, block.index))
                i += 1
                try:
                    if extra_blocks:
                        transaction.extra_blocks = extra_blocks
                    await transaction.verify()
                except InvalidTransactionException as e:
                    self.app_log.warning(e)
                    return False
                except InvalidTransactionSignatureException as e:
                    self.app_log.warning(e)
                    return False
                except MissingInputTransactionException as e:
                    self.app_log.warning(e)
                except NotEnoughMoneyException as e:
                    self.app_log.warning(e)
                    return False
                except Exception as e:
                    self.app_log.warning(e)
                    return False

                if transaction.inputs:
                    failed = False
                    used_ids_in_this_txn = []
                    async for x in get_inputs(transaction.inputs):
                        if self.config.BU.is_input_spent(x.id, transaction.public_key):
                            failed = True
                        if x.id in used_ids_in_this_txn:
                            failed = True
                        if (x.id, transaction.public_key) in used_inputs:
                            failed = True
                        used_inputs[(x.id, transaction.public_key)] = transaction
                        used_ids_in_this_txn.append(x.id)
                    if failed and block.index >= CHAIN.CHECK_DOUBLE_SPEND_FROM:
                        raise MissingInputTransactionException()
                    elif failed and block.index < CHAIN.CHECK_DOUBLE_SPEND_FROM:
                        continue
                        

            if block.index == 0:
                return True
            height = block.index
            last_block = await self.config.mongo.async_db.blocks.find_one({'index': block.index - 1})

            if not last_block:
                self.app_log.warning("Integrate block error 3")
                raise ForkException()

            last_block = await Block.from_dict(last_block)

            if last_block.index != (block.index - 1) or last_block.hash != block.prev_hash:
                self.app_log.warning("Integrate block error 2")
                raise ForkException()

            if height >= CHAIN.FORK_10_MIN_BLOCK:
                target = await BlockFactory.get_target_10min(height, last_block, block)
            else:
                target = await BlockFactory.get_target(height, last_block, block)
            delta_t = int(time()) - int(last_block.time)
            special_target = CHAIN.special_target(block.index, block.target, delta_t, get_config().network)
            target_block_time = CHAIN.target_block_time(self.config.network)

            if block.index >= 35200 and delta_t < 600 and block.special_min:
                raise Exception('Special min block too soon')

            # TODO: use a CHAIN constant for pow blocks limits
            if ((int(block.hash, 16) < target) or
                (block.special_min and int(block.hash, 16) < special_target) or
                (block.special_min and block.index < 35200) or
                (block.index >= 35200 and block.index < 38600 and block.special_min and
                (int(block.time) - int(last_block.time)) > target_block_time)):

                if last_block.index == (block.index - 1) and last_block.hash == block.prev_hash:
                    # self.mongo.db.blocks.update({'index': block.index}, block.to_dict(), upsert=True)
                    # self.mongo.db.blocks.remove({'index': {"$gt": block.index}}, multi=True)
                    # todo: is this useful? can we have more blocks above? No because if we had, we would have raised just above
                    await self.mongo.async_db.block.delete_many({'index': {"$gte": block.index}})
                    db_block = block.to_dict()
                    db_block['updated_at'] = time()
                    await self.mongo.async_db.blocks.replace_one({'index': block.index}, db_block, upsert=True)
                    await self.mongo.async_db.miner_transactions.delete_many({'id': {'$in': [x.transaction_signature for x in block.transactions]}})
                    self.latest_block = await Block.from_dict(await self.config.BU.get_latest_block_async(False))
                    if self.debug:
                        self.app_log.info("New block inserted for height: {}".format(block.index))
                    await self.config.on_new_block(block)  # This will propagate to BU
                    return True
                else:
                    self.app_log.warning("Integrate block error 4")
                    raise ForkException()
            else:
                self.app_log.warning("Integrate block error 5")
                raise AboveTargetException()
            return False  # unreachable code
        except Exception as e:
            if self.config.debug:
                from traceback import format_exc
                self.app_log.warning("{}".format(format_exc()))
            raise

    async def retrace(self, block, peer):
        """We got a non compatible block. Retrace other chains to find a common ancestor and evaluate chains."""
        # TODO: more async conversion TBD here. Low priority since not called often atm.
        # TODO: cleanup print and logging
        # TODO: limit possible retrace blocks vs max(known chains) - store in chain config
        try:
            self.app_log.info("Retracing...")
            blocks = [block]
            while 1:
                if self.debug:
                    self.app_log.info("{} : {}".format(block.hash, block.index))
                # get the previous block from either the consensus collection in mongo
                # or attempt to get the block from the remote peer
                previous_consensus_block = await self.get_previous_consensus_block_from_local(block, peer)
                if previous_consensus_block:
                        block = previous_consensus_block
                        blocks.append(block)
                else:
                    if peer.is_me:
                        self.mongo.db.consensus.update({'peer': peer.to_string(), 'index': {'$gte': block.index}}, {'$set': {'ignore': True}}, multi=True)
                        return
                    try:
                        previous_consensus_block = await self.get_previous_consensus_block_from_remote(block, peer)
                    except BadPeerException as e:
                        self.mongo.db.consensus.update({'peer': peer.to_string(), 'index': {'$gte': block.index}}, {'$set': {'ignore': True}}, multi=True)
                    except:
                        pass
                    if previous_consensus_block and previous_consensus_block.index + 1 == block.index:
                        block = previous_consensus_block
                        blocks.append(block)
                        try:
                            await self.insert_consensus_block(block, peer)
                        except Exception as e:
                            if self.debug:
                                self.app_log.warning("Exception retrace insert_consensus_block: {}".format(e))  # we should do something here to keep it from looping on this failed block
                    else:
                        # identify missing and prune
                        # if the pruned chain is still longer, we'll take it
                        if previous_consensus_block:
                            block = previous_consensus_block
                            blocks = [block]
                        else:
                            return
                latest_block = await self.config.mongo.async_db.blocks.find_one({'index': block.index - 1})
                # if they do have it, query our consensus collection for prevHash of that block, repeat 1 and 2 until index 1
                if latest_block and latest_block['hash'] == block.prev_hash:
                    prev_blocks_check = await Block.from_dict(latest_block)
                    if self.debug:
                        self.app_log.debug("Previous block {}: {}".format(prev_blocks_check.hash, prev_blocks_check.index))
                    blocks = sorted(blocks, key=lambda x: x.index)
                    block_for_next = blocks[-1]
                    while 1:
                        next_block = await self.get_next_consensus_block_from_local(block_for_next)
                        if next_block:
                            blocks.append(next_block)
                            block_for_next = next_block
                        else:
                            break

                    # self.peers.init(self.config.network)
                    remote_blocks = NodeSocket.getblocks(block_for_next.index + 1, block_for_next.index + 100)
                    for remote_block in remote_blocks:
                        if remote_block.prev_hash == block_for_next.hash:
                            blocks.append(remote_block)
                            block_for_next = remote_block
                        else:
                            break

                    # If the block height is equal, we throw out the inbound chain, it muse be greater
                    # If the block height is lower, we throw it out
                    # if the block height is heigher, we compare the difficulty of the entire chain
                    existing_blockchain = await Blockchain.init_async(self.config.mongo.async_db.blocks.find({'index': {'$gte': blocks[0].index}}), partial=True)
                    if existing_blockchain:
                        existing_difficulty = await existing_blockchain.get_difficulty()
                        existing_latest_block = await self.config.BU.get_latest_block_async()
                        existing_blockchain_index = existing_latest_block['index']
                    else:
                        existing_difficulty = 0
                        existing_blockchain_index = latest_block['index']

                    async def get_blocks(blocks):
                        for block in blocks:
                            yield block
                    inbound_blockchain = await Blockchain.init_async(get_blocks(blocks), partial=True)
                    inbound_difficulty = await inbound_blockchain.get_difficulty()

                    if (blocks[-1].index >= existing_blockchain_index
                        and inbound_difficulty >= existing_difficulty):
                        for block in blocks:
                            fork_exception = False
                            try:
                                if block.index == 0:
                                    continue
                                await self.integrate_block_with_existing_chain(block)
                                if self.debug:
                                    self.app_log.debug('inserted {}'.format(block.index))
                            except ForkException as e:
                                fork_exception = True
                            except AboveTargetException as e:
                                return
                            except IndexError as e:
                                return
                        
                            if fork_exception:
                                back_one_block = block
                                while 1:
                                    back_one_block = await self.mongo.async_db.consensus.find_one({'block.hash': back_one_block.prev_hash})
                                    if back_one_block:
                                        back_one_block = await Block.from_dict( back_one_block['block'])
                                        if back_one_block.index < self.latest_block.index: # If its index less than latest, it won't get integrated
                                            break
                                        try:
                                            result = await self.integrate_block_with_existing_chain(back_one_block)
                                            if result:
                                                await self.integrate_block_with_existing_chain(block)
                                                break
                                        except ForkException as e:
                                            pass
                                    else:
                                        return
                        self.app_log.info("Retrace result: replaced chain with incoming")
                        return
                    else:
                        if not peer.is_me:
                            if self.debug:
                                self.app_log.info("Incoming chain lost {} {} {} {}"
                                                  .format(inbound_difficulty, existing_difficulty, blocks[-1].index,
                                                          await self.config.BU.get_latest_block_async().index)
                                                  )
                            for block in blocks:
                                self.mongo.db.consensus.update({'block.hash': block.hash}, {'$set': {'ignore': True}}, multi=True)
                        return
                # lets go down the hash path to see where prevHash is in our blockchain, hopefully before the genesis block
                # we need some way of making sure we have all previous blocks until we hit a block with prevHash in our main blockchain
                #there is no else, we just loop again
                # if we get to index 1 and prev hash doesn't match the genesis, throw out the chain and black list the peer
                # if we get a fork point, prevHash is found in our consensus or genesis, then we compare the current
                # blockchain against the proposed chain.

                # TODO: Here, compare vs current known consensus height, and limit to consensus height - CHAIN.MAX_RETRACE_DEPTH
                if block.index == 0:
                    self.app_log.info("Retrace result: zero index reached")
                    return
            self.app_log.info("Retrace result: doesn't follow any known chain")  # throwing out the block for now
            return
        except Exception as e:
            exc_type, exc_obj, exc_tb = exc_info()
            fname = path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            self.app_log.warning("{} {} {}".format(exc_type, fname, exc_tb.tb_lineno))
            raise
