#!/usr/bin/env python3
"""Get your own Telegram ID and switch a bot to the local Bot API securely."""
import getpass
import json
import sys
from urllib.request import Request, urlopen


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ('id', 'logout'):
        raise SystemExit('Usage: python3 scripts/telegram_setup.py id|logout')
    token = getpass.getpass('BotFather bot token (input hidden): ').strip()
    if not token:
        raise SystemExit('Token is empty')
    action = 'getUpdates' if sys.argv[1] == 'id' else 'logOut'
    req = Request('https://api.telegram.org/bot' + token + '/' + action,
                  data=b'{}', headers={'Content-Type': 'application/json'})
    try:
        with urlopen(req, timeout=20) as res:
            data = json.load(res)
    except Exception:
        raise SystemExit('Telegram request failed. Check the token and network.') from None
    if not data.get('ok'):
        raise SystemExit('Telegram rejected the request. Check that no webhook is active.')
    if action == 'logOut':
        print('Bot logged out of the cloud Bot API; start the local server now.')
    else:
        for update in data['result']:
            message = update.get('message', {})
            if message.get('chat', {}).get('type') == 'private':
                print('Private message from user ID:', message.get('from', {}).get('id'))
        if not data['result']:
            print('No messages yet. Send /id to your bot in Telegram and run this again.')


if __name__ == '__main__':
    main()
