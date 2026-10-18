from contextlib import closing
import datetime
import socket

import google.auth.exceptions
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from gcalcli.printer import Printer


def authenticate(
    client_id: str, client_secret: str, printer: Printer, local: bool
):
    flow = InstalledAppFlow.from_client_config(
        client_config={
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                "redirect_uris": ["http://localhost"],
            }
        },
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    if not local:
        printer.msg(
            'Note: Behavior of the `--noauth-local-server` option has changed! '
            'Starting local server, but providing instructions for connecting '
            'to it remotely...\n'
        )
    credentials = None
    attempt_num = 0
    # Retry up to 5 attempts with different random ports.
    while credentials is None:
        port = _free_local_port()
        if not local:
            printer.msg('Option 1 (outbound):\n', 'yellow')
            printer.msg(
                '  To establish a connection from this system to a remote '
                'host, execute a command like: `ssh username@host -L '
                f'{port}:localhost:{port} BROWSER=open $BROWSER '
                "'https://the-url-below'`\n",
            )
            printer.msg('Option 2 (outbound):\n', 'yellow')
            printer.msg(
                '  To establish a connection from a remote host to this '
                'system, execute a command from remote host like: '
                f'`ssh username@host -fN -R {port}:localhost:{port} ; '
                "BROWSER=open $BROWSER https://the-url-below'`\n\n",
            )
        try:
            credentials = flow.run_local_server(open_browser=False, port=port)
        except OSError as e:
            if e.errno == 98 and attempt_num < 4:
                # Will get retried with a different port.
                printer.msg(f'Port {port} in use, trying another port...')
                attempt_num += 1
            else:
                raise
        except RecursionError:
            raise OSError(
                'Failed to fetch credentials. If this is a nonstandard gcalcli '
                'install, please try again with a system-installed gcalcli as '
                'a workaround.\n'
                'Details: https://github.com/insanum/gcalcli/issues/735.'
            )
    return credentials


def _free_local_port():
    # See https://stackoverflow.com/a/45690594.
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(('', 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def refresh_if_expired(credentials) -> None:
    if credentials.expired:
        credentials.refresh(Request())


# Google expires refresh tokens after 7 days for apps in "Testing" status.
TESTING_LOGIN_DAYS = 7


def is_invalid_grant(exc: google.auth.exceptions.RefreshError) -> bool:
    """Whether Google rejected the saved refresh token itself."""
    details = exc.args[1] if len(exc.args) > 1 else None
    return isinstance(details, dict) and details.get('error') == 'invalid_grant'


def invalid_grant_message(
    signed_in: datetime.datetime, now: datetime.datetime
) -> str:
    """Explain why Google rejected a login saved at `signed_in`, with fixes."""
    days = (now - signed_in).days
    message = (
        "Error: Google rejected gcalcli's saved login from "
        f"{signed_in:%Y-%m-%d}, {days} day{'' if days == 1 else 's'} ago "
        '(invalid_grant).\n'
    )
    if days < TESTING_LOGIN_DAYS:
        return message + (
            'Access may have been revoked: '
            'https://myaccount.google.com/connections\n'
            '  Fix:  gcalcli init\n'
        )
    return message + (
        f'Google ends logins after {TESTING_LOGIN_DAYS} days while your Cloud '
        'app is in "Testing" mode.\n'
        '  Short-term fix:  gcalcli init\n'
        '  Long-term fix:   click "Publish app" at '
        'https://console.developers.google.com/auth/audience\n'
    )


def creds_from_legacy_json(data):
    kwargs = {
        k: v
        for k, v in data.items()
        if k
        in (
            'client_id',
            'client_secret',
            'refresh_token',
            'token_uri',
            'scopes',
        )
    }
    return Credentials(data['access_token'], **kwargs)
