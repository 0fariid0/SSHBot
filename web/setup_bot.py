import asyncio
import json
from .app import telegram, ORIGIN

async def main():
    bot = await telegram('getMe', {})
    hook = await telegram('getWebhookInfo', {})
    if hook.get('url'):
        raise RuntimeError('This bot has an active webhook; use a separate bot token.')
    await telegram('setChatMenuButton', {'menu_button': {'type': 'web_app', 'text': 'SSHBot', 'web_app': {'url': ORIGIN}}})
    await telegram('setMyCommands', {'commands': [{'command': 'start', 'description': 'باز کردن برنامه'}, {'command': 'id', 'description': 'نمایش آیدی تلگرام'}]})
    print('Bot ready: https://t.me/' + bot['username'])

if __name__ == '__main__':
    asyncio.run(main())
