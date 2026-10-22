from ..request import get_image_message
import os
import asyncio
IMAGE_URL = os.getenv("IMAGE_URL")

async def test_image():
    image_message = await get_image_message(IMAGE_URL, engine="gemini")
    print(image_message)

if __name__ == "__main__":
    asyncio.run(test_image())

'''
python -m core.test.test_image
'''