import json
from pathlib import Path
import urllib.request
from .debug import qDebug

try:
    import certifi
except ImportError:
    certifi = None

from . import var


def _make_ssl_context():
    import ssl

    cert = None
    if certifi is not None:
        cert = certifi.where()
    else:
        bundled_ca_bundle = Path(__file__).resolve().with_name("cacert.pem")
        if bundled_ca_bundle.exists():
            cert = str(bundled_ca_bundle)

    return (
        ssl.create_default_context(cafile=cert)
        if cert
        else ssl.create_default_context()
    )


SSL_CONTEXT = _make_ssl_context()


def nxmFetch(requestData):
    jsonData = json.dumps(requestData).encode("utf-8")
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:135.0) Gecko/20211714 Firefox/135.0",
        "Accept": "application/json",
        "Content-type": "application/json",
    }
    request = urllib.request.Request(
        "https://api.nexusmods.com/v2/graphql", data=jsonData, headers=headers
    )
    try:
        with urllib.request.urlopen(request, context=SSL_CONTEXT) as response:
            content = response.read()
            resp = json.loads(content)
            qDebug(var.cleanJson(resp))
            if "errors" in resp:
                qDebug(f"[NXMColDL] API returned errors: {resp['errors']}")
            return resp.get("data")
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        qDebug(f"[NXMColDL] Error fetching data: {str(e)}")
        return None
    except Exception as e:
        qDebug(f"[NXMColDL] Unexpected error: {str(e)}")
        return None


def fetchRevisions(url):
    qDebug(f"[NXMColDL] Fetching revisions for {url}")
    query = """
            query CollectionRevisions($domainName: String, $slug: String!) {
                collection(domainName: $domainName, slug: $slug) {
                    revisions {
                        createdAt
                        revisionNumber
                    }
                }
            }
            """
    jsonData = {
        "query": query,
        "variables": {"domainName": var.game, "slug": var.collection},
        "operationName": "CollectionRevisions",
    }
    return nxmFetch(jsonData)


def fetchInfo(url):
    qDebug(f"[NXMColDL] Fetching collection info for {url}")
    query = """
            query CollectionManifestInfo($domainName: String, $slug: String!) {
                collection(domainName: $domainName, slug: $slug) {
                    name
                    summary
                    user {
                        name
                    }
                    tileImage {
                        thumbnailUrl(size: small)
                    }
                }
            }
            """
    jsonData = {
        "query": query,
        "variables": {"domainName": var.game, "slug": var.collection},
        "operationName": "CollectionManifestInfo",
    }
    return nxmFetch(jsonData)


def fetchModInfo(url):
    qDebug(f"[NXMColDL] Fetching mod info for {url}")
    query = """
            query CollectionRevisionMods($revision: Int, $slug: String!, $viewAdultContent: Boolean) {
                collectionRevision(revision: $revision, slug: $slug, viewAdultContent: $viewAdultContent) {
                    id
                    collectionId
                    revisionNumber
                    modCount
                    totalSize
                    installationInfo
                    gameVersions {
                        reference
                    }
                    externalResources {
                        id
                        name
                        resourceType
                        resourceUrl
                    }
                    modFiles {
                        fileId
                        optional
                        file {
                            fileId
                            name
                            scanned
                            size
                            sizeInBytes
                            version
                            mod {
                                adult
                                author
                                category
                                modId
                                name
                                pictureUrl
                                summary
                                version
                                game {
                                    domainName
                                }
                                uploader {
                                    avatar
                                    memberId
                                    name
                                }
                            }
                        }
                    }
                }
            }
            """
    jsonData = {
        "query": query,
        "variables": {
            "revision": var.revision,
            "slug": var.collection,
            "viewAdultContent": True,
        },
        "operationName": "CollectionRevisionMods",
    }
    return nxmFetch(jsonData)


# ── the collection's own answers ──────────────────────────────────────────────
#
# Nexus publishes a collection's FOMOD answers and its conflict rules only in
# the collection *manifest*, and that document is behind the API key. MO2 has
# the key — it is connected to Nexus — but its Python API has no accessor for
# it, so it is read from the Windows credential store that MO2 itself writes
# (`GlobalSettings::nexusApiKey` reads the credential named "APIKEY"; the name
# is in the MO2 binary). Wine implements the same call, so this works under
# Proton as it does on Windows.


def nexus_key() -> str:
    """MO2's Nexus API key, or "" when it cannot be read.

    Never logged and never written anywhere: it is the operator's own key, and
    the only thing that happens to it is one request header.
    """
    try:
        import ctypes
        from ctypes import wintypes

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        advapi = ctypes.windll.advapi32
        cred = ctypes.POINTER(CREDENTIAL)()
        if not advapi.CredReadW("APIKEY", 1, 0, ctypes.byref(cred)):
            qDebug("[NXMColDL] MO2 has no Nexus API key stored")
            return ""
        try:
            size = cred.contents.CredentialBlobSize
            if not size:
                return ""
            raw = ctypes.string_at(cred.contents.CredentialBlob, size)
        finally:
            advapi.CredFree(cred)
        key = raw.decode("utf-8", "ignore").strip("\x00").strip()
        qDebug(f"[NXMColDL] MO2's Nexus API key: {len(key)} characters")
        return key
    except Exception as e:
        qDebug(f"[NXMColDL] could not read MO2's API key: {e}")
        return ""


def _authed_json(url: str, key: str, headers_extra=None):
    """One GET with the API key, as JSON. `None` when it did not work."""
    headers = {
        "User-Agent": "MO2-NXM-Collections/1.3.0",
        "Accept": "application/json",
        "apikey": key,
        "Authorization": f"Bearer {key}",
    }
    if headers_extra:
        headers.update(headers_extra)
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, context=SSL_CONTEXT, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        qDebug(f"[NXMColDL] {url}: HTTP {e.code}")
    except Exception as e:
        qDebug(f"[NXMColDL] {url}: {e}")
    return None


def fetchManifest(collection_id: int, revision_id: int):
    """The collection's manifest, or `None`.

    The download link answers with a signed URL to the document itself, so this
    is two requests: ask where it is, then fetch it. A manifest that already
    arrived in the first answer is used as it is.
    """
    key = nexus_key()
    if not key or not collection_id or not revision_id:
        return None
    base = f"https://api.nexusmods.com/v2/collections/{collection_id}/revisions/{revision_id}"
    first = _authed_json(f"{base}/download_link", key)
    if not isinstance(first, dict):
        return None
    if "mods" in first:
        return first
    link = first.get("download_link") or first.get("url")
    if not link:
        qDebug(f"[NXMColDL] no manifest link in {list(first)}")
        return None
    return _authed_json(link, key)


def choiceText(blob) -> str:
    """A collection's FOMOD answer, flattened to something a person reads.

    Nexus stores what Vortex decided: a JSON document (sometimes as a string)
    of groups and the option chosen in each. Anything unexpected is shown as it
    is rather than dropped — half a choice is worse than a raw one.
    """
    if blob is None or blob == "":
        return ""
    data = blob
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return data.strip()
    out = []

    def walk(node, depth=0):
        if depth > 6:
            return
        if isinstance(node, dict):
            name = node.get("name") or node.get("group") or node.get("title")
            picked = node.get("choice") or node.get("option") or node.get("selection")
            if isinstance(picked, str) and picked:
                out.append(f"{name}: {picked}" if name else picked)
            for key in ("groups", "options", "choices", "steps", "selections"):
                if key in node:
                    walk(node[key], depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
        elif isinstance(node, str) and node.strip():
            out.append(node.strip())

    walk(data)
    return " · ".join(dict.fromkeys(out))


def choicesFromManifest(manifest) -> dict:
    """`"modId:fileId" -> the collection's choice` from a manifest."""
    out = {}
    for entry in (manifest or {}).get("mods") or []:
        source = entry.get("source") or {}
        if entry.get("choices"):
            out[f"{source.get('modId')}:{source.get('fileId')}"] = entry["choices"]
    return out
