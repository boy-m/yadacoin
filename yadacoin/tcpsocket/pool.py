import json
import traceback
import time
import base58
import hashlib
import binascii

import tornado.ioloop
from tornado.iostream import StreamClosedError

from yadacoin.core.transactionutils import TU
from yadacoin.core.config import Config
from yadacoin.core.chain import CHAIN
from yadacoin.core.config import get_config
from yadacoin.tcpsocket.base import RPCSocketServer
from yadacoin.core.miningpool import MiningPool, InvalidAddressException, Miner


class StratumServer(RPCSocketServer):
    current_header = ''
    config = None

    def __init__(self):
        super(StratumServer, self).__init__()
        self.config = get_config()

    @classmethod
    async def block_checker(cls):
        if not cls.config:
            cls.config = get_config()

        if time.time() - cls.config.mp.block_factory.time > 600:
            await cls.config.mp.refresh()

        if cls.current_header != cls.config.mp.block_factory.header:
            await cls.send_jobs()

    @classmethod
    async def send_jobs(cls):
        if not cls.config:
            cls.config = get_config()
        streams = list(StratumServer.inbound_streams[Miner.__name__].values())
        for stream in streams:
            await cls.send_job(stream)

    @classmethod
    async def send_job(cls, stream):
        job = await cls.config.mp.block_template(stream.peer.agent)
        stream.jobs[job.id] = job
        cls.current_header = cls.config.mp.block_factory.header
        result = {
            'id': job.id,
            'job': job.to_dict()
        }
        rpc_data = {
            'id': 1,
            'method': 'job',
            'jsonrpc': 2.0,
            'result': result
        }
        try:
            await stream.write(
                '{}\n'.format(json.dumps(rpc_data)).encode()
            )
        except StreamClosedError:
            await StratumServer.remove_peer(stream)
        except Exception:
            cls.config.app_log.warning(traceback.format_exc())

    @classmethod
    async def update_miner_count(cls):
        if not cls.config:
            cls.config = get_config()
        await cls.config.mongo.async_db.pool_stats.update_one({
            'stat': 'worker_count'
        }, {
            '$set': {
                'value': len(StratumServer.inbound_streams[Miner.__name__].keys())
            }
        }
        , upsert=True)
        await cls.config.mongo.async_db.pool_stats.update_one({
            'stat': 'miner_count'
        }, {
            '$set': {
                'value': len(list(set([address for address in StratumServer.inbound_streams[Miner.__name__].keys()])))
            }
        }
        , upsert=True)

    @classmethod
    async def remove_peer(cls, stream):
        stream.close()
        if not hasattr(stream, 'peer'):
            return
        if stream.peer.address in StratumServer.inbound_streams[Miner.__name__]:
            del StratumServer.inbound_streams[Miner.__name__][stream.peer.address]
        await StratumServer.update_miner_count()

    async def getblocktemplate(self, body, stream):
        return await StratumServer.config.mp.block_template(stream.peer.info)

    async def get_info(self, body, stream):
        return await StratumServer.config.mp.block_template(stream.peer.info)

    async def get_balance(self, body, stream):
        balance = StratumServer.config.BU.get_wallet_balance(StratumServer.config.address)
        return {
            'balance': balance,
            'unlocked_balance': balance
        }

    async def getheight(self, body, stream):
        return {
            'height': StratumServer.config.LatestBlock.block.index
        }

    async def transfer(self, body, stream):
        for x in body.get('params').get('destinations'):
            result = await TU.send(StratumServer.config, x['address'], x['amount'], from_address=StratumServer.config.address)
            result['tx_hash'] = result['hash']
        return result

    async def get_bulk_payments(self, body, stream):
        result =  []
        for y in body.get('params').get('payment_ids'):
            config = Config.generate(prv=y)
            async for x in StratumServer.config.BU.get_wallet_unspent_transactions(config.address):
                txn = {'amount': 0}
                txn['block_height'] = x['height']
                for j in x['outputs']:
                    if j['to'] == config.address:
                        txn['amount'] += j['value']
                if txn['amount']:
                    result.append(txn)
        return result

    async def submit(self, body, stream):
        nonce = body['params'].get('nonce')
        if type(nonce) is not str:
            result = {'error': True, 'message': 'nonce is wrong data type'}
        if len(nonce) > CHAIN.MAX_NONCE_LEN:
            result = {'error': True, 'message': 'nonce is too long'}
        data = {
            'id': body.get('id'),
            'method': body.get('method'),
            'jsonrpc': body.get('jsonrpc')
        }
        try:
            data['result'] = await StratumServer.config.mp.on_miner_nonce(
                nonce,
                stream.jobs[body['params']['id']],
                miner=stream.peer,
                miner_hash=body['params']['result']
            )
            if not data['result']:
                data['error'] = {'message': 'Invalid hash for current block'}
        except:
            data['result'] = {}
            data['error'] = {'message': 'Invalid hash for current block'}

        await stream.write('{}\n'.format(json.dumps(data)).encode())
        if 'error' in data:
            await StratumServer.send_job(stream)

        await StratumServer.block_checker()

    async def login(self, body, stream):
        await StratumServer.block_checker()
        job = await StratumServer.config.mp.block_template(body['params'].get('agent'))
        if not hasattr(stream, 'jobs'):
            stream.jobs = {}
        stream.jobs[job.id] = job
        result = {
            'id': job.id,
            'job': job.to_dict()
        }
        rpc_data = {
            'id': body.get('id'),
            'method': body.get('method'),
            'jsonrpc': body.get('jsonrpc'),
            'result': result
        }

        try:
            stream.peer = Miner(
                address=body['params'].get('login'),
                agent=body['params'].get('agent')
            )
        except:
            rpc_data['error'] = {'message': 'Invalid wallet address or invalid format'}
        self.config.app_log.info(f'Connected to Miner: {stream.peer.to_json()}')
        StratumServer.inbound_streams[Miner.__name__][stream.peer.address] = stream
        await StratumServer.update_miner_count()
        await stream.write('{}\n'.format(json.dumps(rpc_data)).encode())

    async def keepalived(self, body, stream):
        rpc_data = {
            'id': body.get('id'),
            'method': body.get('method'),
            'jsonrpc': body.get('jsonrpc'),
            'result': {
                'status': 'KEEPALIVED'
            }
        }
        await stream.write('{}\n'.format(json.dumps(rpc_data)).encode())

    @classmethod
    async def status(self):
        return {
            'miners': len(list(set([address for address in StratumServer.inbound_streams[Miner.__name__].keys()]))),
            'workers': len(StratumServer.inbound_streams[Miner.__name__].keys())
        }
