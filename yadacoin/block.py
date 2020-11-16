import json
import hashlib
import base64
import time
import binascii
import pyrx

from sys import exc_info
from os import path
from decimal import Decimal, getcontext
from bitcoin.signmessage import BitcoinMessage, VerifyMessage
from bitcoin.wallet import P2PKHBitcoinAddress
from coincurve.utils import verify_signature
from logging import getLogger

from yadacoin.chain import CHAIN
from yadacoin.config import get_config
from yadacoin.fastgraph import FastGraph
from yadacoin.transaction import (
    TransactionFactory,
    Transaction,
    InvalidTransactionException,
    ExternalInput,
    MissingInputTransactionException
)


def quantize_eight(value):
    value = Decimal(value)
    value = value.quantize(Decimal('0.00000000'))
    return value


class CoinbaseRule1(Exception):
    pass

class CoinbaseRule2(Exception):
    pass

class CoinbaseRule3(Exception):
    pass

class CoinbaseRule4(Exception):
    pass

class RelationshipRule1(Exception):
    pass

class RelationshipRule2(Exception):
    pass

class FastGraphRule1(Exception):
    pass

class FastGraphRule2(Exception):
    pass

class ExternalInputSpentException(Exception):
    pass


class BlockFactory(object):
    pyrx = None
    cores = 10
    @classmethod
    async def generate(cls, config, transactions, public_key, private_key, force_version=None, index=None, force_time=None):
        try:
            mongo = config.mongo
            app_log = getLogger("tornado.application")
            if force_version is None:
                version = CHAIN.get_version_for_height(index)
            else:
                version = force_version
            if force_time:
                xtime = str(int(force_time))
            else:
                xtime = str(int(time.time()))
            index = int(index)
            if index == 0:
                prev_hash = ''
            else:
                prev_hash = config.BU.get_latest_block()['hash']

            transaction_objs = []
            fee_sum = 0.0
            used_sigs = []
            used_inputs = {}
            for txn in transactions:
                if len(used_sigs) > 100:
                    break
                try:
                    if isinstance(txn, FastGraph):
                        transaction_obj = txn
                    else:
                        transaction_obj = FastGraph.from_dict(index, txn)

                    if transaction_obj.transaction_signature in used_sigs:
                        print('duplicate transaction found and removed')
                        continue

                    if not transaction_obj.verify():
                        raise InvalidTransactionException("invalid transactions")

                    used_sigs.append(transaction_obj.transaction_signature)

                except:
                    try:
                        if isinstance(txn, Transaction):
                            transaction_obj = txn
                        else:
                            transaction_obj = Transaction.from_dict(index, txn)

                        if transaction_obj.transaction_signature in used_sigs:
                            print('duplicate transaction found and removed')
                            continue

                        await transaction_obj.verify()
                        used_sigs.append(transaction_obj.transaction_signature)
                    except:
                        raise InvalidTransactionException("invalid transactions")
                try:
                    if int(index) > CHAIN.CHECK_TIME_FROM and (int(transaction_obj.time) > int(xtime) + CHAIN.TIME_TOLERANCE):
                        config.mongo.db.miner_transactions.remove({'id': transaction_obj.transaction_signature}, multi=True)
                        app_log.debug("Block embeds txn too far in the future {} {}".format(xtime, transaction_obj.time))
                        continue
                    
                    if transaction_obj.inputs:
                        failed = False
                        used_ids_in_this_txn = []
                        for x in transaction_obj.inputs:
                            if get_config().BU.is_input_spent(x.id, transaction_obj.public_key):
                                failed = True
                            if x.id in used_ids_in_this_txn:
                                failed = True
                            if (x.id, transaction_obj.public_key) in used_inputs:
                                failed = True
                            used_inputs[(x.id, transaction_obj.public_key)] = transaction_obj
                            used_ids_in_this_txn.append(x.id)
                        if failed:
                            continue
                    
                    transaction_objs.append(transaction_obj)
                    
                    fee_sum += float(transaction_obj.fee)
                except Exception as e:
                    await mongo.async_db.miner_transactions.delete_many({'id': transaction_obj.transaction_signature})
                    if config.debug:
                        app_log.debug('Exception {}'.format(e))
                    else:
                        continue
            
            block_reward = CHAIN.get_block_reward(index)
            coinbase_txn_fctry = await TransactionFactory.construct(
                index,
                public_key=public_key,
                private_key=private_key,
                outputs=[{
                    'value': block_reward + float(fee_sum),
                    'to': str(P2PKHBitcoinAddress.from_pubkey(bytes.fromhex(public_key)))
                }],
                coinbase=True
            )
            coinbase_txn = coinbase_txn_fctry.generate_transaction()
            transaction_objs.append(coinbase_txn)

            transactions = transaction_objs
            block_factory = cls()
            block = await Block.init_async(
                version=version,
                block_time=xtime,
                block_index=index,
                prev_hash=prev_hash,
                transactions=transactions,
                public_key=public_key
            )
            txn_hashes = block.get_transaction_hashes()
            block.set_merkle_root(txn_hashes)
            block.merkle_root = block.verify_merkle_root
            block_factory.block = block
            return block_factory
        except Exception as e:
            import sys, os
            print("Exception {} BlockFactory".format(e))
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            print(exc_type, fname, exc_tb.tb_lineno)
            raise

    @classmethod
    def generate_header(cls, block):
        if int(block.version) < 3:
            return str(block.version) + \
                str(block.time) + \
                block.public_key + \
                str(block.index) + \
                block.prev_hash + \
                '{nonce}' + \
                str(block.special_min) + \
                str(block.target) + \
                block.merkle_root
        else:
            # version 3 block do not contain special_min anymore and have target as 64 hex string
            # print("target", block.target)
            # TODO: somewhere, target is calc with a / and result is float instead of int.
            return str(block.version) + \
                   str(block.time) + \
                   block.public_key + \
                   str(block.index) + \
                   block.prev_hash + \
                   '{nonce}' + \
                   hex(int(block.target))[2:].rjust(64, '0') + \
                   block.merkle_root

    @classmethod
    def generate_hash_from_header(cls, height, header, nonce):
        if not cls.pyrx:
            cls.pyrx = pyrx.PyRX()
        header = header.format(nonce=nonce)
        if height >= CHAIN.RANDOMX_FORK:
            seed_hash = binascii.unhexlify('4181a493b397a733b083639334bc32b407915b9a82b7917ac361816f0a1f5d4d') #sha256(yadacoin65000)
            bh = cls.pyrx.get_rx_hash(header, seed_hash, height)
            hh = binascii.hexlify(bh).decode()
            return hh
        else:
            return hashlib.sha256(hashlib.sha256(header.encode('utf-8')).digest()).digest()[::-1].hex()

    def get_transaction_hashes(self):
        return sorted([str(x.hash) for x in self.block.transactions], key=str.lower)

    def set_merkle_root(self, txn_hashes):
        hashes = []
        for i in range(0, len(txn_hashes), 2):
            txn1 = txn_hashes[i]
            try:
                txn2 = txn_hashes[i+1]
            except:
                txn2 = ''
            hashes.append(hashlib.sha256((txn1+txn2).encode('utf-8')).digest().hex())
        if len(hashes) > 1:
            self.set_merkle_root(hashes)
        else:
            self.merkle_root = hashes[0]
    
    @classmethod
    async def get_target_10min(
        self,
        height,
        last_block,  # This is the latest on chain block we have in db
        block  # This is the block we are currently mining, not on chain yet, with current time in it.
    ):
        # Aim at 5 min average block time, with escape hatch
        max_target = 0x0000ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff  # A single cpu does that under a minute.
        retarget_period = 6 * 5  # 5 hours at 10 min per block - needs to be high enough to account for organic variance of the miners
        retarget_period2 = int(6 * 1.5)  # 1 hour and 30 min at 10 min per block - Faster reaction to drops in blocktime, we want to make "instamine" harder
        target_time = 10 * 60  # 10 min
        # That should not happen
        if int(block.time) - int(last_block.time) > 3600:
            get_config().debug_log("Block time over max. Max target set.")
            return int(max_target)
        # decrease after 2x target - can be 3 as well
        current_block_time = int(block.time) - int(last_block.time)
        adjusted = False
        if current_block_time > 2 * target_time:
            latest_target = last_block.target
            delta = max_target - latest_target
            # Linear decrease to reach max target after one hour block time.
            new_target = int(latest_target + delta * current_block_time / 3600)
            # print("adjust", current_block_time, MinerSimulator.HEX(new_target), latest_target)
            adjusted = new_target
            # To be used later on, once the rest is calc'd
        latest_block = await get_config().BU.get_latest_block_async()
        start_index = latest_block['index']

        block_from_retarget_period_ago = await Block.from_dict(await get_config().mongo.async_db.blocks.find_one({'index': start_index-retarget_period}))
        retarget_period_ago_time = block_from_retarget_period_ago.time
        elapsed_time_from_retarget_period_ago = int(block.time) - int(retarget_period_ago_time)
        average_block_time = elapsed_time_from_retarget_period_ago / retarget_period

        block_from_retarget_period2_ago = await Block.from_dict(await get_config().mongo.async_db.blocks.find_one({'index': start_index-retarget_period2}))
        retarget_period2_ago_time = block_from_retarget_period2_ago.time
        elapsed_time_from_retarget_period2_ago = int(block.time) - int(retarget_period2_ago_time)
        average_block_time2 = elapsed_time_from_retarget_period2_ago / retarget_period2

        # React faster to a drop in block time than to a raise. short block times are more a threat than large ones.
        if average_block_time2 < target_time:
            hash_sum2 = 0
            for i in range(start_index, start_index - retarget_period2, -1):
                this_block = await get_config().mongo.async_db.blocks.find_one({'index': i})
                if this_block:
                    block_tmp = await Block.from_dict(this_block)
                    hash_sum2 += block_tmp.target
            average_target = hash_sum2 / retarget_period2
            target = int(average_target * average_block_time2 / target_time)
        else:
            hash_sum = 0
            for i in range(start_index, start_index - retarget_period, -1):
                this_block = await get_config().mongo.async_db.blocks.find_one({'index': i})
                if this_block:
                    block_tmp = await Block.from_dict(this_block)
                    hash_sum += block_tmp.target
            average_target = hash_sum / retarget_period
            # This adjusts both ways
            target = int(average_target * average_block_time / target_time)
        if adjusted:
            # Take min of calc and adjusted
            if adjusted > target:
                target = adjusted

        get_config().debug_log("average block time {}".format(average_block_time))
        get_config().debug_log("average target {:02x} target {:02x}".format(int(average_target), int(target)))
        if target < 1:
            target = 1
            block.special_min = False

        if target > max_target:
            target = max_target
        return int(target)

    @classmethod
    async def get_target(cls, height, last_block, block) -> int:
        try:
            # change target
            max_target = CHAIN.MAX_TARGET
            if get_config().network in ['regnet', 'testnet']:
                return int(max_target)

            latest_block = await get_config().BU.get_latest_block_async()
            max_block_time = CHAIN.target_block_time(get_config().network)
            retarget_period = CHAIN.RETARGET_PERIOD  # blocks
            max_seconds = CHAIN.TWO_WEEKS  # seconds
            min_seconds = CHAIN.HALF_WEEK  # seconds
            if height >= CHAIN.POW_FORK_V3:
                retarget_period = CHAIN.RETARGET_PERIOD_V3
                max_seconds = CHAIN.MAX_SECONDS_V3  # seconds
                min_seconds = CHAIN.MIN_SECONDS_V3  # seconds
            elif height >= CHAIN.POW_FORK_V2:
                retarget_period = CHAIN.RETARGET_PERIOD_V2
                max_seconds = CHAIN.MAX_SECONDS_V2  # seconds
                min_seconds = CHAIN.MIN_SECONDS_V2  # seconds
            if height > 0 and height % retarget_period == 0:
                get_config().debug_log(
                    "RETARGET get_target height {} - last_block {} - block {}/time {}".format(height, last_block.index, block.index, block.time))
                block_from_2016_ago = await Block.from_dict(get_config().BU.get_block_by_index(height - retarget_period))
                get_config().debug_log(
                    "Block_from_2016_ago - block {}/time {}".format(block_from_2016_ago.index, block_from_2016_ago.time))
                two_weeks_ago_time = block_from_2016_ago.time
                elapsed_time_from_2016_ago = int(last_block.time) - int(two_weeks_ago_time)
                get_config().debug_log("elapsed_time_from_2016_ago {} s {} days".format(int(elapsed_time_from_2016_ago), elapsed_time_from_2016_ago/(60*60*24)))
                # greater than two weeks?
                if elapsed_time_from_2016_ago > max_seconds:
                    time_for_target = max_seconds
                    get_config().debug_log("gt max")
                elif elapsed_time_from_2016_ago < min_seconds:
                    time_for_target = min_seconds
                    get_config().debug_log("lt min")
                else:
                    time_for_target = int(elapsed_time_from_2016_ago)

                block_to_check = last_block

                start_index = latest_block['index']

                get_config().debug_log("start_index {}".format(start_index))
                if block_to_check.special_min or block_to_check.target == max_target or not block_to_check.target:
                    block_to_check = await Block.from_dict(await get_config().mongo.async_db.blocks.find_one({
                        '$and': [
                            {
                                'index': {'$lte': start_index}
                            },
                            {
                                'special_min': False
                            },
                            {
                                'target': { '$ne': hex(max_target)[2:]}
                            }
                        ]
                    }, sort=[('index', -1)]))
                target = block_to_check.target
                get_config().debug_log("start_index2 {}, target {}".format(block_to_check.index, hex(int(target))[2:].rjust(64, '0')))

                new_target = int((time_for_target * target) / max_seconds)
                get_config().debug_log("new_target {}".format(hex(int(new_target))[2:].rjust(64, '0')))

                if new_target > max_target:
                    target = max_target
                else:
                    target = new_target

            elif height == 0:
                target = max_target
            else:
                block_to_check = block
                delta_t = int(block.time) - int(last_block.time)
                if block.index >= 38600 and delta_t > max_block_time and block.special_min:
                    special_target = CHAIN.special_target(block.index, block.target, delta_t, get_config().network)
                    return special_target

                block_to_check = last_block  # this would be accurate. right now, it checks if the current block is under its own target, not the previous block's target

                start_index = latest_block['index']

                while 1:
                    if start_index == 0:
                        return block_to_check.target
                    if block_to_check.special_min or block_to_check.target == max_target or not block_to_check.target:
                        block_to_check = await Block.from_dict(await get_config().mongo.async_db.blocks.find_one({'index': start_index}))
                        start_index -= 1
                    else:
                        target = block_to_check.target
                        break
            return int(target)
        except Exception as e:
            import sys, os
            print("Exception {} get_target".format(e))
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            print(exc_type, fname, exc_tb.tb_lineno)
            raise

    @classmethod
    def mine(cls, height, header, target, nonces, special_min=False, special_target=''):

        lowest = (CHAIN.MAX_TARGET, 0, '')
        nonce = nonces[0]
        while nonce < nonces[1]:

            hash_test = cls.generate_hash_from_header(height, header, str(nonce))
            text_int = int(hash_test, 16)
            if text_int < target or (special_min and text_int < int(special_target, 16)):
                return nonce, hash_test

            if text_int < lowest[0]:
                lowest = (text_int, nonce, hash_test)
            nonce += 1
        return lowest[1], lowest[2]

    @classmethod
    async def get_genesis_block(cls):
        return await Block.from_dict({
            "nonce" : 0,
            "hash" : "0dd0ec9ab91e9defe535841a4c70225e3f97b7447e5358250c2dc898b8bd3139",
            "public_key" : "03f44c7c4dca3a9204f1ba284d875331894ea8ab5753093be847d798274c6ce570",
            "id" : "MEUCIQDDicnjg9DTSnGOMLN3rq2VQC1O9ABDiXygW7QDB6SNzwIga5ri7m9FNlc8dggJ9sDg0QXUugrHwpkVKbmr3kYdGpc=",
            "merkleRoot" : "705d831ced1a8545805bbb474e6b271a28cbea5ada7f4197492e9a3825173546",
            "index" : 0,
            "target" : "fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
            "special_min" : False,
            "version" : "1",
            "transactions" : [ 
                {
                    "public_key" : "03f44c7c4dca3a9204f1ba284d875331894ea8ab5753093be847d798274c6ce570",
                    "fee" : 0.0000000000000000,
                    "hash" : "71429326f00ba74c6665988bf2c0b5ed9de1d57513666633efd88f0696b3d90f",
                    "dh_public_key" : "",
                    "relationship" : "",
                    "inputs" : [],
                    "outputs" : [ 
                        {
                            "to" : "1iNw3QHVs45woB9TmXL1XWHyKniTJhzC4",
                            "value" : 50.0000000000000000
                        }
                    ],
                    "rid" : "",
                    "id" : "MEUCIQDZbaCDMmJJ+QJHldj1EWu0yG7enlwRAXoO1/B617KaxgIgBLB4L2ICWpDZf5Eo2bcXgUmKd91ayrOG/6jhaIZAPb0="
                }
            ],
            "time" : "1537127756",
            "prevHash" : ""
        })


class Block(object):

    # Memory optimization
    __slots__ = ('app_log', 'config', 'mongo', 'version', 'time', 'index', 'prev_hash', 'nonce', 'transactions', 'txn_hashes',
                 'merkle_root', 'verify_merkle_root','hash', 'public_key', 'signature', 'special_min', 'target',
                 'special_target', 'header')
    
    @classmethod
    async def init_async(
        cls,
        version=0,
        block_time=0,
        block_index=-1,
        prev_hash='',
        nonce:str='',
        transactions=None,
        block_hash='',
        merkle_root='',
        public_key='',
        signature='',
        special_min: bool=False,
        header= '',
        target: int=0,
        special_target: int=0
    ):
        self = cls()
        self.app_log = getLogger('tornado.application')
        self.config = get_config()
        self.mongo = self.config.mongo
        self.version = version
        self.time = block_time
        self.index = block_index
        self.prev_hash = prev_hash
        self.nonce = nonce
        self.transactions = transactions
        # txn_hashes = self.get_transaction_hashes()
        # self.set_merkle_root(txn_hashes)
        self.merkle_root = merkle_root
        self.verify_merkle_root = ''
        self.hash = block_hash
        self.public_key = public_key
        self.signature = signature
        self.special_min = special_min
        self.target = target
        self.special_target = special_target
        if target==0:
            # Same call as in new block check - but there's a circular reference here.
            latest_block = self.config.BU.get_latest_block()
            if not latest_block:
                self.target = CHAIN.MAX_TARGET
            else:
                if self.index >= CHAIN.FORK_10_MIN_BLOCK:
                    self.target = await BlockFactory.get_target_10min(self.index, await Block.from_dict(latest_block), self)
                else:
                    self.target = await BlockFactory.get_target(self.index, await Block.from_dict(latest_block), self)
            self.special_target = self.target
            # TODO: do we need recalc special target here if special min?
        self.header = header
        return self

    async def copy(self):
        return await Block.init_async(self.version, self.time, self.index, self.prev_hash, self.nonce, self.transactions,
                     self.hash, self.merkle_root, self.public_key, self.signature, self.special_min,
                     self.header, self.target, self.special_target)

    @classmethod
    async def from_dict(cls, block):
        transactions = []
        for txn in block.get('transactions'):
            # TODO: do validity checking for coinbase transactions
            if str(P2PKHBitcoinAddress.from_pubkey(bytes.fromhex(block.get('public_key')))) in [x['to'] for x in txn.get('outputs', '')] and len(txn.get('outputs', '')) == 1 and not txn.get('inputs') and not txn.get('relationship'):
                txn['coinbase'] = True  
            else:
                txn['coinbase'] = False
            if 'signatures' in txn:
                transactions.append(FastGraph.from_dict(block.get('index'), txn))
            else:
                transactions.append(Transaction.from_dict(block.get('index'), txn))

        if block.get('special_target', 0) == 0:
            block['special_target'] = block.get('target')

        return await cls.init_async(
            version=block.get('version'),
            block_time=block.get('time'),
            block_index=block.get('index'),
            public_key=block.get('public_key'),
            prev_hash=block.get('prevHash'),
            nonce=block.get('nonce'),
            transactions=transactions,
            block_hash=block.get('hash'),
            merkle_root=block.get('merkleRoot'),
            signature=block.get('id'),
            special_min=block.get('special_min'),
            header=block.get('header', ''),
            target=int(block.get('target'), 16),
            special_target=int(block.get('special_target', 0), 16)
        )
    
    def get_coinbase(self):
        for txn in self.transactions:
            if str(P2PKHBitcoinAddress.from_pubkey(bytes.fromhex(self.public_key))) in [x.to for x in txn.outputs] and len(txn.outputs) == 1 and not txn.relationship and len(txn.inputs) == 0:
                return txn

    def generate_hash_from_header(self, height, header, nonce):
        if not BlockFactory.pyrx:
            BlockFactory.pyrx = pyrx.PyRX()
        header = header.format(nonce=nonce)
        if height >= CHAIN.RANDOMX_FORK:
            seed_hash = binascii.unhexlify('4181a493b397a733b083639334bc32b407915b9a82b7917ac361816f0a1f5d4d') #sha256(yadacoin65000)
            bh = BlockFactory.pyrx.get_rx_hash(header, seed_hash, height)
            hh = binascii.hexlify(bh).decode()
            return hh
        else:
            return hashlib.sha256(hashlib.sha256(header.encode('utf-8')).digest()).digest()[::-1].hex()

    def verify(self):
        try:
            getcontext().prec = 8
            if int(self.version) != int(CHAIN.get_version_for_height(self.index)):
                raise Exception("Wrong version for block height", self.version, CHAIN.get_version_for_height(self.index))

            txns = self.get_transaction_hashes()
            self.set_merkle_root(txns)
            if self.verify_merkle_root != self.merkle_root:
                raise Exception("Invalid block merkle root")

            header = BlockFactory.generate_header(self)
            hashtest = self.generate_hash_from_header(self.index, header, str(self.nonce))
            # print("header", header, "nonce", self.nonce, "hashtest", hashtest)
            if self.hash != hashtest:
                getLogger("tornado.application").warning("Verify error hashtest {} header {} nonce {}".format(hashtest, header, self.nonce))
                raise Exception('Invalid block hash')

            address = P2PKHBitcoinAddress.from_pubkey(bytes.fromhex(self.public_key))
            try:
                # print("address", address, "sig", self.signature, "pubkey", self.public_key)
                result = verify_signature(base64.b64decode(self.signature), self.hash.encode('utf-8'), bytes.fromhex(self.public_key))
                if not result:
                    raise Exception("block signature1 is invalid")
            except:
                try:
                    result = VerifyMessage(address, BitcoinMessage(self.hash.encode('utf-8'), magic=''), self.signature)
                    if not result:
                        raise
                except:
                    raise Exception("block signature2 is invalid")

            # verify reward
            coinbase_sum = 0
            for txn in self.transactions:
                if int(self.index) > CHAIN.CHECK_TIME_FROM and (int(txn.time) > int(self.time) + CHAIN.TIME_TOLERANCE):
                    #self.config.mongo.db.miner_transactions.remove({'id': txn.transaction_signature}, multi=True)
                    #raise Exception("Block embeds txn too far in the future")
                    pass

                if txn.coinbase:
                    for output in txn.outputs:
                        coinbase_sum += float(output.value)

            fee_sum = 0.0
            for txn in self.transactions:
                if not txn.coinbase:
                    fee_sum += float(txn.fee)
            reward = CHAIN.get_block_reward(self.index)

            #if Decimal(str(fee_sum)[:10]) != Decimal(str(coinbase_sum)[:10]) - Decimal(str(reward)[:10]):
            """
            KO for block 13949
            0.02099999 50.021 50.0
            Integrate block error 1 ('Coinbase output total does not equal block reward + transaction fees', 0.020999999999999998, 0.021000000000000796)
            """
            if quantize_eight(fee_sum) != quantize_eight(coinbase_sum - reward):
                print(fee_sum, coinbase_sum, reward)
                raise Exception("Coinbase output total does not equal block reward + transaction fees", fee_sum, (coinbase_sum - reward))
        except Exception as e:
            exc_type, exc_obj, exc_tb = exc_info()
            fname = path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            getLogger("tornado.application").warning("verify {} {} {}".format(exc_type, fname, exc_tb.tb_lineno))
            raise

    def get_transaction_hashes(self):
        """Returns a sorted list of tx hash, so the merkle root is constant across nodes"""
        return sorted([str(x.hash) for x in self.transactions], key=str.lower)

    def set_merkle_root(self, txn_hashes):
        hashes = []
        for i in range(0, len(txn_hashes), 2):
            txn1 = txn_hashes[i]
            if len(txn_hashes)-1 < i+1:
                txn2 = ''
            else:
                txn2 = txn_hashes[i+1]
            hashes.append(hashlib.sha256((txn1+txn2).encode('utf-8')).digest().hex())
        if len(hashes) > 1:
            self.set_merkle_root(hashes)
        else:
            self.verify_merkle_root = hashes[0]

    async def save(self):
        self.verify()
        for txn in self.transactions:
            if txn.inputs:
                failed = False
                used_ids_in_this_txn = []
                for x in txn.inputs:
                    if self.config.BU.is_input_spent(x.id, txn.public_key):
                        failed = True
                    if x.id in used_ids_in_this_txn:
                        failed = True
                    used_ids_in_this_txn.append(x.id)
                if failed:
                    raise Exception('double spend', [x.id for x in txn.inputs])
        res = self.mongo.db.blocks.find({"index": (int(self.index) - 1)})
        if res.count() and res[0]['hash'] == self.prev_hash or self.index == 0:
            self.mongo.db.blocks.insert(self.to_dict())
        else:
            print("CRITICAL: block rejected...")

    def delete(self):
        self.mongo.db.blocks.remove({"index": self.index})

    def to_dict(self):
        try:
            return {
                'version': self.version,
                'time': self.time,
                'index': self.index,
                'public_key': self.public_key,
                'prevHash': self.prev_hash,
                'nonce': self.nonce,
                'transactions': [x.to_dict() for x in self.transactions],
                'hash': self.hash,
                'merkleRoot': self.merkle_root,
                'special_min': self.special_min,
                'target': hex(self.target)[2:].rjust(64, '0'),
                'special_target': hex(self.special_target)[2:].rjust(64, '0'),
                'header': self.header,
                'id': self.signature
            }
        except Exception as e:
            print(e)
            print("target", self.target, "spec", self.special_target)

    def to_json(self):
        return json.dumps(self.to_dict(), indent=4)

    def in_the_future(self):
        """Tells wether the block is too far away in the future"""
        return int(self.time) > time.time() + CHAIN.TIME_TOLERANCE
