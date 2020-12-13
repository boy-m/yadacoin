import unittest
from unittest import IsolatedAsyncioTestCase # python 3.8 requiredsudo apt install python3.8
import test_setup
from yadacoin.core.blockchain import Blockchain
from yadacoin.core.block import Block
import yadacoin.core.config


class TestBlockchain(IsolatedAsyncioTestCase):
    async def test_init_async(self):
        blockchain = await Blockchain.init_async()
        self.assertIsInstance(blockchain, Blockchain)
    
    async def test_make_gen(self):
        blockchain = await Blockchain.init_async()
        block_gen = blockchain.make_gen([1, 2])
        self.assertEqual([x async for x in block_gen], [1, 2])
    
    async def test_blocks(self):
        blocks = [Block()]
        blockchain = await Blockchain.init_async(blocks)
        self.assertEqual(
            [x async for x in blockchain.blocks],
            blocks
        )
    
    async def test_get_block(self):
        blocks = [Block()]
        blockchain = await Blockchain.init_async(blocks)
        got_block = await blockchain.get_block(0,1)
        self.assertEqual(
            got_block,
            blocks[0]
        )
    
    async def test_get_blocks(self):
        blocks = [Block(), Block()]
        blockchain = await Blockchain.init_async(blocks)
        got_blocks = blockchain.get_blocks(0,2)
        self.assertEqual(
            [x async for x in got_blocks],
            blocks
        )
    
    async def test_is_consecutive(self):
        block1 = Block()
        block1.index = 0
        block1.hash = '3'
        block1.prev_hash = ''

        block2 = Block()
        block2.index = 1
        block2.hash = '4'
        block2.prev_hash = '3'
        blockchain = await Blockchain.init_async([block1, block2])
        self.assertTrue(await blockchain.is_consecutive)
    
    async def test_final_block(self):
        blocks = [Block(), Block()]
        blockchain = await Blockchain.init_async(blocks)
        final_block = await blockchain.final_block
        self.assertEqual(
            final_block,
            blocks[1]
        )
    
    async def test_count(self):
        blocks = [Block(), Block()]
        blockchain = await Blockchain.init_async(blocks)
        final_block = await blockchain.final_block
        self.assertEqual(
            final_block,
            blocks[1]
        )
    
    async def test_verify(self):
        from traceback import format_exc
        blockchain = await Blockchain.init_async()
        try:
            await blockchain.verify()
        except:
            self.fail(f'Blockchain did not verify {format_exc()}')
    
    async def test_test_inbound_chain(self):
        block1 = Block()
        block1.index = 0
        block1.hash = '3000000000000000'
        block1.prev_hash = ''

        block2 = Block()
        block2.index = 1
        block2.hash = '4000000000000000'
        blocks = [block1, block2]
        blockchain = await Blockchain.init_async(blocks)

        block1 = Block()
        block1.index = 0
        block1.hash = '3'
        block1.prev_hash = ''

        block2 = Block()
        block2.index = 1
        block2.hash = '5'

        block3 = Block()
        block3.index = 2
        block3.hash = '5'
        inbound_blocks = [block1, block2, block3]
        inbound_blockchain = await Blockchain.init_async(inbound_blocks)
        
        self.assertTrue(
            await blockchain.test_inbound_blockchain(
                inbound_blockchain
            )
        )
    
    async def test_get_difficulty(self):
        block1 = Block()
        block1.index = 0
        block1.hash = '3000000000000000'
        block1.prev_hash = ''

        block2 = Block()
        block2.index = 1
        block2.hash = '4000000000000000'
        blocks = [block1, block2]
        blockchain = await Blockchain.init_async(blocks)
        result = await blockchain.get_difficulty()
        self.assertEqual(
            result, 
            231584178474632390847141970017375815706539969331281128078907097565294011351038
        )



if __name__ == '__main__':
    unittest.main(argv=['first-arg-is-ignored'], exit=False)