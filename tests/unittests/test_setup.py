import sys
import os.path
import json
import tornado
from random import randrange

from tornado import testing

from yadacoin.core.transaction import Transaction
from yadacoin.core.chain import CHAIN
from yadacoin.app import NodeApplication
from yadacoin.core.blockchain import Blockchain
from yadacoin.core.block import Block
from yadacoin.core.config import CONFIG, Config
import yadacoin.core.config


yadacoin.core.config.CONFIG = Config.generate()


class BaseTestCase(testing.AsyncHTTPTestCase):
    def get_new_ioloop(self):
        return tornado.ioloop.IOLoop.current()

    def get_app(self):
        return NodeApplication()

    async def create_fork_block(self):
        return await Block.generate(
            self._app.config,
            [],
            self._app.config.public_key,
            self._app.config.private_key,
            index=124499,
            force_version=4,
            nonce=1,
            prev_hash='',
            target=CHAIN.MAX_TARGET
        )

    async def create_blockchain(self, start_index, num_blocks, fork_block):
        blocks = []

        for i in range(num_blocks):
            if i == 0:
                block = await Block.generate(
                    self._app.config,
                    [],
                    self._app.config.public_key,
                    self._app.config.private_key,
                    index=start_index,
                    force_version=4,
                    nonce=randrange(1, 1000000),
                    target=CHAIN.MAX_TARGET - 1,
                    prev_hash=fork_block.hash
                )
            else:
                block = await Block.generate(
                    self._app.config,
                    [],
                    self._app.config.public_key,
                    self._app.config.private_key,
                    index=start_index + i,
                    force_version=4,
                    nonce=randrange(1, 1000000),
                    target=CHAIN.MAX_TARGET - 1,
                    prev_hash=block.hash
                )
            blocks.append(block)

        return await Blockchain.init_async(blocks, partial=True)
    
    async def sort_blockchains_by_difficulty(self, *args):
        return [x['blockchain'] for x in sorted([
            {
                'blockchain': x, 
                'difficulty': await x.get_difficulty()
            } for x in args
        ], key=lambda bc: bc['difficulty'])]
