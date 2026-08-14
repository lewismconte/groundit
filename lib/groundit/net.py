"""HTTP transport, working in both worlds.

Two transports, tried in order, because neither is reliable everywhere:

  .NET  - preferred inside Revit. Picks up corporate proxy settings from the
          system for free, which matters a lot in an architecture office, and
          HttpWebRequest is synchronous by design. That last part is a
          feature: HttpClient's async API blocked with .Result on Revit's UI
          thread is a textbook deadlock that only shows up on someone else's
          machine.
  urllib - headless CPython, and a genuine fallback when the .NET types
          cannot be resolved.

The fallback is not decorative. On .NET 8 (Revit 2025+) the WebRequest types
live in System.Net.Requests rather than the System assembly, so a bare
"from System.Net import WebRequest" can fail even though "import System.Net"
succeeded. That failure mode produced a "cannot reach the elevation service"
dialog on a machine with perfectly good internet, so:

  - assemblies are referenced explicitly before the types are imported;
  - a .NET failure falls through to urllib instead of ending the run;
  - the error text from every attempt is kept and shown, rather than being
    flattened into a guess about the user's firewall.
"""

import time

from . import USER_AGENT

try:                                    # Python 2 / IronPython
    from urllib import quote as _quote
except ImportError:                     # Python 3
    from urllib.parse import quote as _quote


class NetworkError(Exception):
    """Any transport failure, already phrased for a user-facing dialog."""


# Assemblies holding the types we need. Names differ between .NET Framework
# and .NET Core, so ask for all of them and ignore the ones that are absent.
_ASSEMBLIES = ("System", "System.Net.Requests", "System.Net.Primitives",
               "System.Runtime", "System.IO.Compression")

_dotnet = None                          # None = unprobed, False = no, dict = types
_dotnet_error = ""


def _dotnet_types():
    """Resolve the .NET HTTP types once. -> dict of types, or None."""
    global _dotnet, _dotnet_error
    if _dotnet is not None:
        return _dotnet or None

    notes = []
    try:
        import clr
        for name in _ASSEMBLIES:
            try:
                clr.AddReference(name)
            except Exception as exc:
                notes.append("%s (%s)" % (name, type(exc).__name__))
    except ImportError:
        _dotnet = False
        _dotnet_error = "no clr module (not running on .NET)"
        return None

    try:
        from System.Net import WebRequest
        from System.IO import MemoryStream
        from System.Text import Encoding
    except Exception as exc:
        _dotnet = False
        _dotnet_error = "%s: %s" % (type(exc).__name__, exc)
        if notes:
            _dotnet_error += " | assemblies not referenced: " + ", ".join(notes)
        return None

    # Optional extras: nice to have, never worth failing over.
    decompression = None
    try:
        from System.Net import DecompressionMethods
        decompression = DecompressionMethods
    except Exception:
        pass

    _dotnet = {"WebRequest": WebRequest, "MemoryStream": MemoryStream,
               "Encoding": Encoding, "DecompressionMethods": decompression}
    return _dotnet


def transport_report():
    """Which transport is available, and why the other is not. -> str.

    Shown when a download fails, so a support conversation starts from facts.
    """
    types = _dotnet_types()
    lines = []
    if types:
        lines.append(".NET transport: available"
                     + ("" if types["DecompressionMethods"] else " (no gzip)"))
    else:
        lines.append(".NET transport: unavailable - %s" % (_dotnet_error or "unknown"))
    try:
        try:
            from urllib.request import urlopen  # noqa: F401
        except ImportError:
            from urllib2 import urlopen  # noqa: F401
        lines.append("urllib transport: available")
    except Exception as exc:
        lines.append("urllib transport: unavailable - %s" % exc)
    return "\n".join(lines)


# ------------------------------------------------------------------- .NET

def _dotnet_fetch(url, body=None, timeout=60):
    types = _dotnet_types()
    if not types:
        raise NetworkError(".NET transport unavailable: %s" % _dotnet_error)

    WebRequest = types["WebRequest"]
    MemoryStream = types["MemoryStream"]
    Encoding = types["Encoding"]

    req = WebRequest.Create(url)
    req.Timeout = int(timeout * 1000)
    req.UserAgent = USER_AGENT

    # These two are HttpWebRequest-only. Setting them is worthwhile - Overpass
    # JSON compresses by roughly ten to one - but never worth failing over.
    try:
        req.ReadWriteTimeout = int(timeout * 1000)
    except Exception:
        pass
    if types["DecompressionMethods"] is not None:
        try:
            methods = types["DecompressionMethods"]
            req.AutomaticDecompression = methods.GZip | methods.Deflate
        except Exception:
            pass

    if body is not None:
        raw = Encoding.UTF8.GetBytes(body)
        req.Method = "POST"
        req.ContentType = "application/x-www-form-urlencoded"
        req.ContentLength = raw.Length
        stream = req.GetRequestStream()
        try:
            stream.Write(raw, 0, raw.Length)
        finally:
            stream.Close()
    else:
        req.Method = "GET"

    response = req.GetResponse()
    try:
        buf = MemoryStream()
        rs = response.GetResponseStream()
        try:
            rs.CopyTo(buf)
        finally:
            rs.Close()
        return bytearray(buf.ToArray())
    finally:
        response.Close()


# ---------------------------------------------------------------- urllib

def _urllib_fetch(url, body=None, timeout=60):
    try:
        from urllib.request import Request, urlopen
    except ImportError:
        from urllib2 import Request, urlopen

    data = body.encode("utf-8") if body is not None else None
    headers = {"User-Agent": USER_AGENT}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = Request(url, data=data, headers=headers)
    handle = urlopen(req, timeout=timeout)
    try:
        return bytearray(handle.read())
    finally:
        handle.close()


# ------------------------------------------------------------------ public

def _transports():
    """Transports to try, best first."""
    if _dotnet_types():
        return [(".NET", _dotnet_fetch), ("urllib", _urllib_fetch)]
    return [("urllib", _urllib_fetch), (".NET", _dotnet_fetch)]


def fetch(url, body=None, timeout=60, retries=2, backoff=2.0):
    """Fetch a URL, POSTing body if given. -> bytearray.

    Tries every available transport before giving up, and reports what each
    one actually said. Raises NetworkError.
    """
    errors = []
    for label, impl in _transports():
        for attempt in range(retries + 1):
            try:
                return impl(url, body=body, timeout=timeout)
            except Exception as exc:
                errors.append("%s: %s: %s" % (label, type(exc).__name__, exc))
                if attempt < retries:
                    time.sleep(backoff * (attempt + 1))
        # Out of retries on this transport; fall through to the next one
        # rather than giving up on the request.
    raise NetworkError("%s\n\n%s" % (url, "\n".join(errors[-4:])))


def form_body(fields):
    """Encode fields as an x-www-form-urlencoded body. -> str."""
    return "&".join("%s=%s" % (k, _quote(str(v).encode("utf-8"), safe=""))
                    for k, v in fields.items())


def fetch_form(url, fields, timeout=90, retries=1):
    """POST an x-www-form-urlencoded body. -> bytearray."""
    return fetch(url, body=form_body(fields), timeout=timeout, retries=retries)


def fetch_text(url, body=None, timeout=60, retries=2):
    """Fetch and decode as UTF-8. -> unicode text.

    Always UTF-8, never the platform default. OSM is full of non-ASCII names
    and letting the platform codec guess is how you get a crash on the one
    building with an accent in it.
    """
    return bytes(fetch(url, body=body, timeout=timeout, retries=retries)).decode(
        "utf-8", "replace")


def fetch_first(urls, body=None, timeout=90, retries=0):
    """Try each mirror in turn. -> (bytearray, url_that_worked).

    Overpass rate limits hard, and the main endpoint is the busiest; falling
    through to a mirror turns a failed run into a slow one.
    """
    errors = []
    for url in urls:
        try:
            return fetch(url, body=body, timeout=timeout, retries=retries), url
        except Exception as exc:
            errors.append("%s: %s" % (url, exc))
    raise NetworkError("Every endpoint failed.\n\n" + "\n".join(errors))


PROBE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/0/0/0.png"


def check_reachable(url=PROBE_URL, timeout=10):
    """Cheap pre-flight. -> (ok, detail).

    detail carries the real reason on failure. The whole point: a transport
    bug and a blocked firewall are different problems and must not produce the
    same message.
    """
    try:
        fetch(url, timeout=timeout, retries=0)
        return True, ""
    except Exception as exc:
        return False, "%s\n\n%s" % (exc, transport_report())


def reachable(url=PROBE_URL, timeout=10):
    """-> True if the elevation service answered."""
    return check_reachable(url, timeout)[0]
