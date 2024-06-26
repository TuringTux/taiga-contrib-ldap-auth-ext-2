# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from typing import Any

from ldap3 import Server, Connection, AUTO_BIND_NO_TLS, AUTO_BIND_TLS_BEFORE_BIND, ANONYMOUS, SIMPLE, SYNC, SUBTREE, NONE

from django.conf import settings
from ldap3.utils.conv import escape_filter_chars
from taiga.base.connectors.exceptions import ConnectorBaseException


class LDAPError(ConnectorBaseException):
    pass


class LDAPConnectionError(LDAPError):
    pass


class LDAPUserLoginError(LDAPError):
    pass


# TODO https://github.com/Monogramm/taiga-contrib-ldap-auth-ext/issues/16
SERVER = getattr(settings, "LDAP_SERVER", "localhost")
PORT = getattr(settings, "LDAP_PORT", "389")

SEARCH_BASE = getattr(settings, "LDAP_SEARCH_BASE", "")
SEARCH_FILTER_ADDITIONAL = getattr(
    settings, "LDAP_SEARCH_FILTER_ADDITIONAL", "")
BIND_DN = getattr(settings, "LDAP_BIND_DN", "")
BIND_PASSWORD = getattr(settings, "LDAP_BIND_PASSWORD", "")

USERNAME_ATTRIBUTE = getattr(settings, "LDAP_USERNAME_ATTRIBUTE", "uid")
EMAIL_ATTRIBUTE = getattr(settings, "LDAP_EMAIL_ATTRIBUTE", "mail")
FULL_NAME_ATTRIBUTE = getattr(settings, "LDAP_FULL_NAME_ATTRIBUTE", "displayName")

TLS_CERTS = getattr(settings, "LDAP_TLS_CERTS", "")
START_TLS = getattr(settings, "LDAP_START_TLS", False)


def _get_server() -> Server:
    """
    Connect to an LDAP server (no authentication yet).
    """
    tls = TLS_CERTS or None
    use_ssl = SERVER.lower().startswith("ldaps://")

    try:
        server = Server(SERVER, port=PORT, get_info=NONE,
                        use_ssl=use_ssl, tls=tls)
    except Exception as e:
        error = "Error connecting to LDAP server: %s" % e
        raise LDAPConnectionError({"error_message": error})


def _get_auth_details(username_sanitized: str) -> dict[str, Any]:
    if BIND_DN and "<username>" in BIND_DN:
        # Authenticate using the provided user credentials
        user = BIND_DN.replace("<username>", username_sanitized)
        password = password
        authentication = SIMPLE
    elif BIND_DN:
        # Authenticate with dedicated bind credentials
        user = BIND_DN
        password = BIND_PASSWORD
        authentication = SIMPLE
    else:
        # Use anonymous auth
        user = None
        password = None
        authentication = ANONYMOUS

    return {
        "user": user,
        "password": password,
        "authentication": authentication
    }


def login(username_or_email: str, password: str) -> tuple[str, str, str]:
    """
    Connect to LDAP server, perform a search and attempt a bind.

    Can raise `exc.LDAPConnectionError` exceptions if the
    connection to LDAP fails.

    Can raise `exc.LDAPUserLoginError` exceptions if the
    login to LDAP fails.

    :param username_or_email: a possibly unsanitized username or email
    :param password: a possibly unsanitized password
    :returns: tuple (username, email, full_name)
    """
    server = _get_server()    
    username_or_email_sanitized = escape_filter_chars(username_or_email)

    auto_bind = AUTO_BIND_NO_TLS
    if START_TLS:
        auto_bind = AUTO_BIND_TLS_BEFORE_BIND

    try:
        c = Connection(server, auto_bind=auto_bind, client_strategy=SYNC, check_names=True,
                       **_get_auth_details(username_or_email_sanitized))
    except Exception as e:
        error = "Error connecting to LDAP server: %s" % e
        raise LDAPConnectionError({"error_message": error})

    # search for user-provided login
    search_filter = '(|(%s=%s)(%s=%s))' % (
        USERNAME_ATTRIBUTE, username_or_email_sanitized, EMAIL_ATTRIBUTE, username_or_email_sanitized)
    if SEARCH_FILTER_ADDITIONAL:
        search_filter = '(&%s%s)' % (search_filter, SEARCH_FILTER_ADDITIONAL)
    try:
        c.search(search_base=SEARCH_BASE,
                 search_filter=search_filter,
                 search_scope=SUBTREE,
                 attributes=[USERNAME_ATTRIBUTE,
                             EMAIL_ATTRIBUTE, FULL_NAME_ATTRIBUTE],
                 paged_size=5)
    except Exception as e:
        error = "LDAP login incorrect: %s" % e
        raise LDAPUserLoginError({"error_message": error})

    # we are only interested in user objects in the response
    c.response = [r for r in c.response if 'raw_attributes' in r and 'dn' in r]
    # stop if no search results
    if not c.response:
        raise LDAPUserLoginError({"error_message": "LDAP login not found"})

    # handle multiple matches
    if len(c.response) > 1:
        raise LDAPUserLoginError(
            {"error_message": "LDAP login could not be determined."})

    # handle missing mandatory attributes
    raw_attributes = c.response[0].get('raw_attributes')
    if not (raw_attributes.get(USERNAME_ATTRIBUTE) and
            raw_attributes.get(EMAIL_ATTRIBUTE) and
            raw_attributes.get(FULL_NAME_ATTRIBUTE)):
        raise LDAPUserLoginError({"error_message": "LDAP login is invalid."})

    # attempt LDAP bind
    username = raw_attributes.get(USERNAME_ATTRIBUTE)[0].decode('utf-8')
    email = raw_attributes.get(EMAIL_ATTRIBUTE)[0].decode('utf-8')
    full_name = raw_attributes.get(FULL_NAME_ATTRIBUTE)[0].decode('utf-8')
    try:
        dn = str(bytes(c.response[0].get('dn'), 'utf-8'), encoding='utf-8')
        Connection(server, auto_bind=auto_bind, client_strategy=SYNC,
                   check_names=True, authentication=SIMPLE,
                   user=dn, password=password)
    except Exception as e:
        error = "LDAP bind failed: %s" % e
        raise LDAPUserLoginError({"error_message": error})

    # LDAP binding successful, but some values might have changed, or
    # this is the user's first login, so return them
    return (username, email, full_name)
