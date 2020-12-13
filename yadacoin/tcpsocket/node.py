import base64
import json
import time
from uuid import uuid4

from tornado.iostream import StreamClosedError
from coincurve import verify_signature

from yadacoin.tcpsocket.base import RPCSocketServer, RPCSocketClient, BaseRPC
from yadacoin.core.chain import CHAIN
from yadacoin.core.block import Block
from yadacoin.core.latestblock import LatestBlock
from yadacoin.core.peer import Peer, Seed, SeedGateway, ServiceProvider, User
from yadacoin.core.config import get_config
from yadacoin.core.transactionutils import TU
from yadacoin.core.identity import Identity
from yadacoin.core.transaction import Transaction
from yadacoin.core.blockchain import Blockchain


class NodeRPC(BaseRPC):
    config = None
    async def getblocks(self, body, stream):
        # get blocks should be done only by syncing peers
        params = body.get('params')
        start_index = int(params.get("start_index", 0))
        end_index = min(int(params.get("end_index", 0)), start_index + CHAIN.MAX_BLOCKS_PER_MESSAGE)
        if start_index > self.config.LatestBlock.block.index:
            result = []
        else:
            blocks = self.config.mongo.async_db.blocks.find({
                '$and': [
                    {'index':
                        {'$gte': start_index}

                    },
                    {'index':
                        {'$lte': end_index}
                    }
                ]
            }, {'_id': 0}).sort([('index',1)])
            result = await blocks.to_list(length=CHAIN.MAX_BLOCKS_PER_MESSAGE)
        
        await self.write_result(
            stream,
            'blocksresponse',
            {
                'blocks': result
            },
            body['id']
        )

    async def service_provider_request(self, body, stream):
        payload = body.get('params', {})
        if (
            not payload.get('seed_gateway')
        ):
            return
        seed_gateway = SeedGateway.from_dict(payload.get('seed_gateway'))
        if (
            self.config.peer.__class__ == SeedGateway and
            self.config.peer.identity.username_signature == seed_gateway.identity.username_signature
        ):
            service_provider = None
            for x, service_provider in self.config.nodeServer.inbound_streams[ServiceProvider.__name__].items():
                break
            
            if not service_provider:
                return
            payload[service_provider.peer.source_property] = service_provider.peer.to_dict()
            return await self.write_params(
                stream,
                'service_provider_request',
                payload
            )
        payload2 = payload.copy()
        payload2.setdefault(self.config.peer.source_property, self.config.peer.to_dict())
        async for peer_stream in self.config.peer.get_service_provider_request_peers(stream.peer, payload):
            try:
                await self.write_params(
                    peer_stream,
                    'service_provider_request',
                    payload2
                )
            except:
                await peer_stream.write_params(
                    'service_provider_request',
                    payload2
                )

    async def newtxn(self, body, stream):
        payload = body.get('params', {})
        if (
            not payload.get('transaction')
        ):
            return

        txn = Transaction.from_dict(payload.get('transaction'))
        try:
            await txn.verify()
        except:
            return

        to_store = txn.to_dict()
        to_store['sent_to'] = [stream.peer.to_dict()]
        await self.config.mongo.async_db.miner_transactions.replace_one(
            {
                'id': txn.transaction_signature
            },
            to_store,
            upsert=True,
        )

        async for peer_stream in self.config.peer.get_sync_peers():
            if peer_stream.peer.rid == stream.peer.rid:
                continue
            await self.write_params(
                peer_stream,
                'newtxn',
                payload
            )

    async def newblock(self, body, stream):
        payload = body.get('params', {}).get('payload')
        if payload.get('block'):
            block = await Block.from_dict(payload.get('block'))
            await self.config.consensus.insert_consensus_block(block, stream.peer)
            await self.ensure_previous_block(block, stream)

    async def ensure_previous_block(self, block, stream):
        have_prev = await self.config.mongo.async_db.blocks.find_one({
            'hash': block.prev_hash
        })
        if not have_prev:
            have_prev = await self.config.mongo.async_db.consensus.find_one({
                'block.hash': block.prev_hash
            })
            if not have_prev:
                await self.write_params(
                    stream,
                    'getblock',
                    {
                        'hash': block.prev_hash
                    }
                )
                return False
        return True

    async def ensure_previous_on_blockchain(self, block):
        return await self.config.mongo.async_db.blocks.find_one({
            'hash': block.prev_hash
        })

    async def send_block(self, block):
        async for peer_stream in self.config.peer.get_sync_peers():
            await self.config.nodeShared().write_params(
                peer_stream,
                'newblock',
                {
                    'payload': {
                        'block': block.to_dict()
                    }
                }
            )

    async def getblock(self, body, stream):
        # get blocks should be done only by syncing peers
        params = body.get('params')
        block_hash = params.get("hash")
        block = await self.config.mongo.async_db.blocks.find_one({'hash': block_hash}, {'_id': 0})
        if not block:
            block = await self.config.mongo.async_db.consensus.find_one({'block.hash': block_hash}, {'_id': 0})
            if block:
                block = block['block']
        if block:
            await self.write_result(stream, 'blockresponse', {
                'block': block
            }, body['id'])

    async def blocksresponse(self, body, stream):
        # get blocks should be done only by syncing peers
        result = body.get('result')
        blocks = result.get('blocks')
        if not blocks:
            self.config.consensus.syncing = False
            stream.synced = True
            return
        self.config.consensus.syncing = True
        
        inbound_blockchain = await Blockchain.init_async(blocks, partial=True)
        existing_blockchain = await Blockchain.init_async(self.config.mongo.async_db.blocks.find({'index': {'$gte': blocks[0]['index']}}), partial=True)
        prev_block = await self.ensure_previous_block(await Block.from_dict(blocks[0]), stream)
        if not prev_block:
            return False
        if await existing_blockchain.test_inbound_blockchain(inbound_blockchain):
            await self.config.consensus.integrate_blockchain_with_existing_chain(inbound_blockchain, stream)
        self.config.consensus.syncing = False

    async def blockresponse(self, body, stream):
        # get blocks should be done only by syncing peers
        result = body.get('result')
        block = await Block.from_dict(result.get("block"))
        await self.config.consensus.insert_consensus_block(block, stream.peer)
        prev_block = await self.ensure_previous_block(block, stream)

        fork_block = await self.ensure_previous_on_blockchain(block)
        if fork_block:
            fork_block = await Block.from_dict(fork_block)
            # ensure_previous_on_blockchain is true, so we have the 
            # linking block from our existing chain.
            local_chain = await self.config.consensus.build_local_chain(block)
            remote_chain = await self.config.consensus.build_remote_chain(block)
            
            await local_chain.test_inbound_blockchain(remote_chain)
        elif prev_block:
            await self.config.consensus.build_backward_from_block_to_fork(block, [], stream)

    async def connect(self, body, stream):
        params = body.get('params')
        if not params.get('peer'):
            stream.close()
            return {}
        generic_peer = Peer.from_dict(params.get('peer'))
        if isinstance(self.config.peer, Seed):

            if generic_peer.identity.username_signature in self.config.seeds:
                peerCls = Seed
            elif generic_peer.identity.username_signature in self.config.seed_gateways:
                peerCls = SeedGateway

        elif isinstance(self.config.peer, SeedGateway):

            if generic_peer.identity.username_signature in self.config.seeds:
                peerCls = Seed
            elif generic_peer.identity.username_signature in self.config.service_providers:
                peerCls = ServiceProvider

        elif isinstance(self.config.peer, ServiceProvider):

            if generic_peer.identity.username_signature in self.config.seed_gateways:
                peerCls = SeedGateway
            else:
                peerCls = User

        elif isinstance(self.config.peer, User):

            peerCls = User
        else:
            self.config.app_log.error('inbound peer is not defined, disconnecting')
            stream.close()
            return {}

        limit = self.config.peer.__class__.type_limit(peerCls)
        if (len(NodeSocketServer.inbound_pending[peerCls.__name__]) + len(NodeSocketServer.inbound_streams[peerCls.__name__])) >= limit:
            await self.write_result(stream, 'capacity', {}, body['id'])
            stream.close()
            return {}

        try:
            stream.peer = peerCls.from_dict(params.get('peer'))
        except:
            self.config.app_log.error('invalid peer identity')
            stream.close()
            return {}

        if generic_peer.rid in NodeSocketServer.inbound_pending[stream.peer.__class__.__name__]:
            stream.close()
            return {}

        if generic_peer.rid in NodeSocketServer.inbound_streams[stream.peer.__class__.__name__]:
            stream.close()
            return {}

        if generic_peer.rid in self.config.nodeClient.outbound_ignore[stream.peer.__class__.__name__]:
            stream.close()
            return

        if generic_peer.rid in self.config.nodeClient.outbound_pending[stream.peer.__class__.__name__]:
            stream.close()
            return

        if generic_peer.rid in self.config.nodeClient.outbound_streams[stream.peer.__class__.__name__]:
            stream.close()
            return

        try:
            result = verify_signature(
                base64.b64decode(stream.peer.identity.username_signature),
                stream.peer.identity.username.encode(),
                bytes.fromhex(stream.peer.identity.public_key)
            )
            if result:
                self.config.app_log.info('new {} peer is valid'.format(stream.peer.__class__.__name__))
        except:
            self.config.app_log.error('invalid peer identity signature')
            stream.close()
            return {}

        NodeSocketServer.inbound_streams[peerCls.__name__][stream.peer.rid] = stream
        self.config.app_log.info('Connected to {}: {}'.format(stream.peer.__class__.__name__, stream.peer.to_json()))
        return {}

    async def challenge(self, body, stream):
        challenge = body.get('params', {}).get('token')
        signed_challenge = TU.generate_signature(challenge, self.config.private_key)
        await self.write_result(stream, 'authenticate', {
            'peer': self.config.peer.to_dict(),
            'signed_challenge': signed_challenge
        }, body['id'])

        stream.peer.token = str(uuid4())
        await self.write_params(stream, 'challenge', {
            'token': stream.peer.token
        })

    async def authenticate(self, body, stream):
        signed_challenge = body.get('result', {}).get('signed_challenge')
        result = verify_signature(
            base64.b64decode(signed_challenge),
            stream.peer.token.encode(),
            bytes.fromhex(stream.peer.identity.public_key)
        )
        if result:
            stream.peer.authenticated = True
            self.config.app_log.info('Authenticated {}: {}'.format(stream.peer.__class__.__name__, stream.peer.to_json()))
        else:
            stream.close()


class NodeSocketServer(RPCSocketServer, NodeRPC):

    def __init__(self):
        super(NodeSocketServer, self).__init__()
        self.config = get_config()


class NodeSocketClient(RPCSocketClient, NodeRPC):

    def __init__(self):
        super(NodeSocketClient, self).__init__()
        self.config = get_config()

    async def connect(self, peer: Peer):
        try:
            stream = await super(NodeSocketClient, self).connect(peer)
            if stream:
                await self.write_params(stream, 'connect', {
                    'peer': self.config.peer.to_dict()
                })

                stream.peer.token = str(uuid4())
                await self.write_params(stream, 'challenge', {
                    'token': stream.peer.token
                })

                await self.wait_for_data(stream)
        except StreamClosedError:
            pass
            #get_config().app_log.error('Cannot connect to {}: {}'.format(peer.__class__.__name__, peer.to_json()))
    
    async def challenge(self, body, stream):
        challenge =  body.get('params', {}).get('token')
        signed_challenge = TU.generate_signature(challenge, self.config.private_key)
        await self.write_result(stream, 'authenticate', {
            'peer': self.config.peer.to_dict(),
            'signed_challenge': signed_challenge
        }, body['id'])
    
    async def capacity(self, body, stream):
        NodeSocketClient.outbound_ignore[stream.peer.__class__.__name__][stream.peer.rid] = stream.peer
        self.config.app_log.warning('{} at full capacity: {}'.format(stream.peer.__class__.__name__, stream.peer.to_json()))
