"""Host-supplied identifier rules. OpenCode prefixes session/message/shell IDs;
Codex uses lowercase UUID thread and turn IDs plus ledger-owned client message IDs."""
import re

OPENCODE = 'opencode'
CODEX = 'codex'
MAX_ID = 256
CODEX_CLIENT_PREFIX = 'ari-cm-'
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_CODEX_CLIENT_MESSAGE = re.compile(r'ari-cm-[0-9a-f_-]+')


def valid_session_id(host, value):
    """Codex sessions are thread IDs (lowercase UUID); other hosts use ses_."""
    if host == CODEX:
        return isinstance(value, str) and _UUID.fullmatch(value) is not None
    return isinstance(value, str) and value.startswith('ses_') and len(value) <= MAX_ID


def valid_message_id(host, value):
    """Codex messages use the ledger-owned client format; other hosts use msg_."""
    if host == CODEX:
        return (isinstance(value, str) and len(value) <= MAX_ID
                and _CODEX_CLIENT_MESSAGE.fullmatch(value) is not None)
    return isinstance(value, str) and value.startswith('msg_') and len(value) <= MAX_ID


def valid_shell_id(host, value):
    if host == CODEX:
        return False  # Codex has no shell-ID scheme yet.
    return isinstance(value, str) and value.startswith('sh_') and len(value) <= MAX_ID


def valid_turn_id(host, value):
    """Codex turns are turn IDs (lowercase UUID); other hosts send host tokens."""
    if host == CODEX:
        return isinstance(value, str) and _UUID.fullmatch(value) is not None
    return isinstance(value, str) and bool(value) and len(value) <= MAX_ID


def new_message_id(host, suffix):
    return CODEX_CLIENT_PREFIX + suffix if host == CODEX else 'msg_research_' + suffix
