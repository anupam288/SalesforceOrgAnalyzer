"""
tools/salesforce_client.py

Authenticated Salesforce client supporting all three auth methods:
  A) Username + Password + Security Token
  B) OAuth Connected App (client_id + client_secret)
  C) JWT Bearer (client_id + private_key_file + username)

Wraps:
  - REST API      — describe, SOQL queries
  - Metadata API  — retrieve all metadata types (ZIP-based)
  - Tooling API   — Apex source code with full Body field
"""
import json
import time
import hashlib
import zipfile
import io
import base64
import logging
import warnings
import requests
import urllib3
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass, field

# Suppress InsecureRequestWarning when verify=False
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# DATA MODEL
# ─────────────────────────────────────────────────────────────────────

@dataclass
class MetadataComponent:
    metadata_type: str
    api_name: str
    label: str = ""
    namespace: str = ""
    last_modified: str = ""
    last_modified_by: str = ""
    created_date: str = ""
    raw_body: str = ""
    attributes: dict = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        prefix = f"{self.namespace}__" if self.namespace else ""
        return f"{prefix}{self.api_name}"

    def to_dict(self) -> dict:
        return {
            "metadata_type": self.metadata_type,
            "api_name": self.api_name,
            "full_name": self.full_name,
            "label": self.label,
            "last_modified": self.last_modified,
            "raw_body": self.raw_body,
            "attributes": self.attributes,
        }


# ─────────────────────────────────────────────────────────────────────
# CLIENT
# ─────────────────────────────────────────────────────────────────────

class SalesforceClient:
    """
    Unified Salesforce client. Call connect() before any other method.

    Auth is selected automatically based on what's in the config:
      - client_id + private_key_file → JWT Bearer
      - client_id + client_secret    → OAuth Client Credentials
      - username + password          → Username/Password flow
    """

    TOKEN_URL = "https://login.salesforce.com/services/oauth2/token"
    # For sandbox orgs use: https://test.salesforce.com/services/oauth2/token

    def __init__(self, config, cache_dir: str = ".cache/metadata"):
        self.config = config
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._access_token: Optional[str] = None
        self._instance_url: Optional[str] = None
        self._api_base: Optional[str] = None

        self._last_request_time: float = 0.0
        self._min_interval: float = 0.2   # 5 req/sec default

        # Shared session with SSL verification disabled — required in
        # corporate proxy environments where SSL is intercepted.
        self._session = requests.Session()
        self._session.verify = False

    # ─────────────────────────────────────────────────────────────────
    # CONNECTION / AUTH
    # ─────────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Authenticate to Salesforce. Must be called before any API call."""
        method = self.config.auth_method
        logger.info(f"Connecting to Salesforce ({method}) → {self.config.instance_url}")

        if method == "jwt":
            self._connect_jwt()
        elif method == "oauth_client_credentials":
            self._connect_oauth_client_credentials()
        else:
            self._connect_username_password()

        self._api_base = f"{self._instance_url}/services/data/v{self.config.api_version}"
        logger.info(f"Connected ✅  instance: {self._instance_url}")

    def _connect_username_password(self) -> None:
        """OAuth Resource Owner Password Credentials flow."""
        params = {
            "grant_type":    "password",
            "client_id":     self.config.client_id or "PlatformCLI",  # fallback to SF CLI app id
            "client_secret": self.config.client_secret or "",
            "username":      self.config.username,
            "password":      self.config.password_with_token,
        }
        self._do_token_request(params)

    def _connect_oauth_client_credentials(self) -> None:
        """
        OAuth 2.0 Client Credentials flow (Connected App).

        Requires in the Connected App:
          - Enable Client Credentials Flow
          - Set a Run As user
          - IP relaxed or agent IP whitelisted
        """
        params = {
            "grant_type":    "client_credentials",
            "client_id":     self.config.client_id,
            "client_secret": self.config.client_secret,
        }
        # Use the org's instance URL as token endpoint (not login.salesforce.com)
        # because client_credentials must hit the org directly
        token_url = f"{self.config.instance_url}/services/oauth2/token"
        self._do_token_request(params, token_url=token_url)

    def _connect_jwt(self) -> None:
        """JWT Bearer Token flow — no interactive login required."""
        import jwt as pyjwt   # pip install PyJWT cryptography
        import datetime

        key_path = Path(self.config.private_key_file)
        if not key_path.exists():
            raise FileNotFoundError(
                f"JWT private key not found: {key_path}\n"
                f"Generate with: openssl genrsa -out server.key 2048"
            )
        private_key = key_path.read_text()

        payload = {
            "iss": self.config.client_id,
            "sub": self.config.username,
            "aud": "https://login.salesforce.com",
            "exp": int((datetime.datetime.utcnow() + datetime.timedelta(minutes=3)).timestamp()),
        }
        token = pyjwt.encode(payload, private_key, algorithm="RS256")

        params = {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion":  token,
        }
        self._do_token_request(params)

    def _do_token_request(self, params: dict, token_url: str = None) -> None:
        """POST to the token endpoint and store access_token + instance_url."""
        url = token_url or self.TOKEN_URL
        try:
            resp = self._session.post(url, data=params, timeout=30)
        except requests.ConnectionError as e:
            raise ConnectionError(
                f"Cannot reach Salesforce token endpoint: {url}\n"
                f"Check your instance_url and network connection.\n"
                f"Error: {e}"
            )

        if resp.status_code != 200:
            body = resp.json() if resp.content else {}
            error = body.get("error", "unknown_error")
            desc  = body.get("error_description", resp.text[:300])
            raise ConnectionError(
                f"Salesforce authentication failed ({resp.status_code}): {error}\n"
                f"{desc}\n\n"
                f"Auth method: {self.config.auth_method}\n"
                + self._auth_troubleshoot_hint()
            )

        data = resp.json()
        self._access_token = data["access_token"]
        self._instance_url = data.get("instance_url", self.config.instance_url).rstrip("/")

    def _auth_troubleshoot_hint(self) -> str:
        method = self.config.auth_method
        if method == "oauth_client_credentials":
            return (
                "OAuth Client Credentials checklist:\n"
                "  1. Connected App → Enable Client Credentials Flow ✓\n"
                "  2. Connected App → Set a 'Run As' user ✓\n"
                "  3. Connected App → IP Relaxation: Relax IP restrictions ✓\n"
                "  4. Connected App is approved (not pending) ✓\n"
                "  5. client_id = Consumer Key, client_secret = Consumer Secret ✓\n"
                "  6. instance_url must be the org URL, NOT login.salesforce.com ✓"
            )
        elif method == "username_password":
            return (
                "Username/Password checklist:\n"
                "  1. security_token: reset at Setup > My Personal Info > Reset Token ✓\n"
                "  2. Or whitelist your IP in Setup > Network Access ✓\n"
                "  3. User must not have MFA enforced ✓\n"
                "  4. If sandbox, add '.invalid' suffix to username (e.g. user@org.com.sandbox) ✓"
            )
        elif method == "jwt":
            return (
                "JWT Bearer checklist:\n"
                "  1. Connected App → Use Digital Signatures, upload server.crt ✓\n"
                "  2. User pre-authorized the Connected App ✓\n"
                "  3. private_key_file points to the matching server.key ✓"
            )
        return ""

    # ─────────────────────────────────────────────────────────────────
    # HTTP HELPERS
    # ─────────────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        if not self._access_token:
            raise RuntimeError("Not connected. Call connect() first.")
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type":  "application/json",
        }

    def _rate_limit(self) -> None:
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_time = time.time()

    def _get(self, path: str, params: dict = None) -> dict:
        self._rate_limit()
        url = f"{self._api_base}{path}"
        resp = self._session.get(url, headers=self._headers(), params=params, timeout=60)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict) -> dict:
        self._rate_limit()
        url = f"{self._api_base}{path}"
        resp = self._session.post(url, headers=self._headers(), json=body, timeout=120)
        resp.raise_for_status()
        return resp.json()

    def _post_soap(self, endpoint: str, soap_body: str) -> str:
        """POST a SOAP envelope and return raw XML response."""
        self._rate_limit()
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type":  "text/xml; charset=UTF-8",
            "SOAPAction":    '""',
        }
        resp = self._session.post(endpoint, headers=headers, data=soap_body.encode("utf-8"), timeout=120)
        resp.raise_for_status()
        return resp.text

    # ─────────────────────────────────────────────────────────────────
    # CACHE HELPERS
    # ─────────────────────────────────────────────────────────────────

    def _cache_path(self, key: str) -> Path:
        hashed = hashlib.md5(key.encode()).hexdigest()
        return self.cache_dir / f"{hashed}.json"

    def _from_cache(self, key: str) -> Optional[Any]:
        p = self._cache_path(key)
        if p.exists():
            with open(p) as f:
                logger.debug(f"Cache hit: {key[:60]}")
                return json.load(f)
        return None

    def _to_cache(self, key: str, data: Any) -> None:
        with open(self._cache_path(key), "w") as f:
            json.dump(data, f, default=str)

    # ─────────────────────────────────────────────────────────────────
    # TOOLING API — APEX SOURCE CODE
    # ─────────────────────────────────────────────────────────────────

    def retrieve_apex_source(self, apex_type: str) -> list[MetadataComponent]:
        """
        Retrieve full Apex source for all classes or triggers.
        Uses Tooling API which returns the Body field (actual source code).
        """
        cache_key = f"apex_source_{apex_type}"
        if cached := self._from_cache(cache_key):
            return [MetadataComponent(**c) for c in cached]

        logger.info(f"Retrieving {apex_type} source via Tooling API...")
        self._rate_limit()

        soql = (
            f"SELECT+Id,Name,Body,LengthWithoutComments,"
            f"LastModifiedDate,LastModifiedBy.Name,CreatedDate,ApiVersion,Status"
            f"+FROM+{apex_type}+ORDER+BY+Name"
        )
        tooling_url = f"{self._instance_url}/services/data/v{self.config.api_version}/tooling/query"
        resp = self._session.get(
            tooling_url,
            headers=self._headers(),
            params={"q": soql.replace("+", " ")},
            timeout=120,
        )
        resp.raise_for_status()
        records = resp.json().get("records", [])
        logger.info(f"  Retrieved {len(records)} {apex_type} records")

        components = []
        for r in records:
            comp = MetadataComponent(
                metadata_type=apex_type,
                api_name=r.get("Name", ""),
                last_modified=r.get("LastModifiedDate", ""),
                last_modified_by=(r.get("LastModifiedBy") or {}).get("Name", ""),
                created_date=r.get("CreatedDate", ""),
                raw_body=r.get("Body", ""),
                attributes={
                    "id": r.get("Id"),
                    "api_version": r.get("ApiVersion"),
                    "status": r.get("Status"),
                    "length_without_comments": r.get("LengthWithoutComments"),
                },
            )
            components.append(comp)

        self._to_cache(cache_key, [c.to_dict() for c in components])
        return components

    # ─────────────────────────────────────────────────────────────────
    # METADATA API (SOAP) — FLOWS AND ALL OTHER TYPES
    # ─────────────────────────────────────────────────────────────────

    def list_metadata(self, metadata_type: str) -> list[dict]:
        """List all components of a given metadata type via Metadata API SOAP."""
        cache_key = f"list_{metadata_type}"
        if cached := self._from_cache(cache_key):
            return cached

        logger.info(f"Listing {metadata_type}...")
        self._rate_limit()

        soap_endpoint = f"{self._instance_url}/services/Soap/m/{self.config.api_version}"
        soap_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope
  xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns:met="http://soap.sforce.com/2006/04/metadata">
  <soapenv:Header>
    <met:CallOptions/>
    <met:SessionHeader>
      <met:sessionId>{self._access_token}</met:sessionId>
    </met:SessionHeader>
  </soapenv:Header>
  <soapenv:Body>
    <met:listMetadata>
      <met:queries>
        <met:type>{metadata_type}</met:type>
      </met:queries>
      <met:asOfVersion>{self.config.api_version}</met:asOfVersion>
    </met:listMetadata>
  </soapenv:Body>
</soapenv:Envelope>"""

        try:
            xml_resp = self._post_soap(soap_endpoint, soap_body)
            items = self._parse_list_metadata_response(xml_resp)
            logger.info(f"  Found {len(items)} {metadata_type} components")
            self._to_cache(cache_key, items)
            return items
        except Exception as e:
            logger.warning(f"  listMetadata failed for {metadata_type}: {e}")
            return []

    def _parse_list_metadata_response(self, xml_text: str) -> list[dict]:
        """Parse the SOAP listMetadata response XML into a list of dicts."""
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_text)
        ns = {
            "soapenv": "http://schemas.xmlsoap.org/soap/envelope/",
            "met":     "http://soap.sforce.com/2006/04/metadata",
        }
        items = []
        # Handle both direct result and list result
        for result in root.iter("{http://soap.sforce.com/2006/04/metadata}result"):
            item = {}
            for child in result:
                tag = child.tag.split("}")[-1]  # strip namespace
                item[tag] = child.text
            if item:
                items.append(item)
        return items

    def retrieve_flows(self) -> list[MetadataComponent]:
        """Retrieve all Flow definitions including XML source."""
        cache_key = "flows_with_source"
        if cached := self._from_cache(cache_key):
            return [MetadataComponent(**c) for c in cached]

        flow_list = self.list_metadata("Flow")
        logger.info(f"Retrieving source for {len(flow_list)} flows...")

        components = []
        batch_size = 10
        for i in range(0, len(flow_list), batch_size):
            batch = flow_list[i : i + batch_size]
            members = [f.get("fullName", "") for f in batch if f.get("fullName")]
            if not members:
                continue

            self._rate_limit()
            try:
                zip_bytes = self._retrieve_metadata_zip("Flow", members)
                with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                    for name in zf.namelist():
                        if name.endswith(".flow-meta.xml") or name.endswith(".flow"):
                            raw_name = Path(name).stem.replace(".flow-meta", "")
                            xml_content = zf.read(name).decode("utf-8", errors="replace")
                            meta = next(
                                (f for f in batch if f.get("fullName", "").endswith(raw_name)),
                                {}
                            )
                            components.append(MetadataComponent(
                                metadata_type="Flow",
                                api_name=meta.get("fullName", raw_name),
                                last_modified=meta.get("lastModifiedDate", ""),
                                last_modified_by=meta.get("lastModifiedByName", ""),
                                raw_body=xml_content,
                            ))
            except Exception as e:
                logger.warning(f"  Flow batch {i} retrieve error: {e}")
                for m in members:
                    components.append(MetadataComponent(
                        metadata_type="Flow", api_name=m, raw_body="",
                        attributes={"error": str(e)},
                    ))

        logger.info(f"  Retrieved {len(components)} flows")
        self._to_cache(cache_key, [c.to_dict() for c in components])
        return components

    def retrieve_metadata_type(self, metadata_type: str) -> list[MetadataComponent]:
        """Generic ZIP-based retrieval for any metadata type."""
        cache_key = f"generic_{metadata_type}"
        if cached := self._from_cache(cache_key):
            return [MetadataComponent(**c) for c in cached]

        members_list = self.list_metadata(metadata_type)
        if not members_list:
            return []

        components = []
        for i in range(0, min(len(members_list), 500), 10):
            batch = members_list[i : i + 10]
            members = [m.get("fullName", "") for m in batch if m.get("fullName")]
            if not members:
                continue
            self._rate_limit()
            try:
                zip_bytes = self._retrieve_metadata_zip(metadata_type, members)
                with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                    for name in zf.namelist():
                        if not name.endswith("/"):
                            content = zf.read(name).decode("utf-8", errors="replace")
                            api_name = Path(name).name.split(".")[0]
                            meta = next(
                                (m for m in batch if api_name in m.get("fullName", "")),
                                {},
                            )
                            components.append(MetadataComponent(
                                metadata_type=metadata_type,
                                api_name=meta.get("fullName", api_name),
                                last_modified=meta.get("lastModifiedDate", ""),
                                raw_body=content,
                            ))
            except Exception as e:
                logger.warning(f"  {metadata_type} batch {i} error: {e}")

        self._to_cache(cache_key, [c.to_dict() for c in components])
        return components

    def _retrieve_metadata_zip(self, metadata_type: str, members: list[str]) -> bytes:
        """
        Call the Metadata API retrieve() SOAP operation and return raw ZIP bytes.
        """
        members_xml = "\n".join(f"<met:members>{m}</met:members>" for m in members)
        soap_endpoint = f"{self._instance_url}/services/Soap/m/{self.config.api_version}"

        # Step 1: Start async retrieve
        retrieve_soap = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope
  xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns:met="http://soap.sforce.com/2006/04/metadata">
  <soapenv:Header>
    <met:SessionHeader>
      <met:sessionId>{self._access_token}</met:sessionId>
    </met:SessionHeader>
  </soapenv:Header>
  <soapenv:Body>
    <met:retrieve>
      <met:retrieveRequest>
        <met:apiVersion>{self.config.api_version}</met:apiVersion>
        <met:unpackaged>
          <met:types>
            {members_xml}
            <met:name>{metadata_type}</met:name>
          </met:types>
        </met:unpackaged>
      </met:retrieveRequest>
    </met:retrieve>
  </soapenv:Body>
</soapenv:Envelope>"""

        xml_resp = self._post_soap(soap_endpoint, retrieve_soap)
        retrieve_id = self._extract_xml_text(xml_resp, "id")
        if not retrieve_id:
            raise ValueError(f"No retrieve ID in response: {xml_resp[:500]}")

        # Step 2: Poll for completion
        for attempt in range(20):
            time.sleep(2 + attempt * 0.5)
            status_xml = self._check_retrieve_status(soap_endpoint, retrieve_id)
            done = self._extract_xml_text(status_xml, "done")
            if done == "true":
                zip_b64 = self._extract_xml_text(status_xml, "zipFile")
                if not zip_b64:
                    raise ValueError("Retrieve completed but no zipFile in response")
                return base64.b64decode(zip_b64)

        raise TimeoutError(f"Metadata retrieve timed out after 20 polls for {metadata_type}")

    def _check_retrieve_status(self, soap_endpoint: str, retrieve_id: str) -> str:
        soap = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope
  xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns:met="http://soap.sforce.com/2006/04/metadata">
  <soapenv:Header>
    <met:SessionHeader>
      <met:sessionId>{self._access_token}</met:sessionId>
    </met:SessionHeader>
  </soapenv:Header>
  <soapenv:Body>
    <met:checkRetrieveStatus>
      <met:asyncProcessId>{retrieve_id}</met:asyncProcessId>
      <met:includeZip>true</met:includeZip>
    </met:checkRetrieveStatus>
  </soapenv:Body>
</soapenv:Envelope>"""
        return self._post_soap(soap_endpoint, soap)

    def _extract_xml_text(self, xml_text: str, tag: str) -> Optional[str]:
        import re
        m = re.search(rf"<(?:\w+:)?{tag}>(.*?)</(?:\w+:)?{tag}>", xml_text, re.DOTALL)
        return m.group(1).strip() if m else None

    # ─────────────────────────────────────────────────────────────────
    # CONVENIENCE METHODS
    # ─────────────────────────────────────────────────────────────────

    def get_validation_rules(self) -> list[MetadataComponent]:
        return self.retrieve_metadata_type("ValidationRule")

    def get_approval_processes(self) -> list[MetadataComponent]:
        return self.retrieve_metadata_type("ApprovalProcess")

    def get_named_credentials(self) -> list[dict]:
        return self.list_metadata("NamedCredential")

    def get_remote_site_settings(self) -> list[dict]:
        return self.list_metadata("RemoteSiteSetting")

    def get_custom_labels(self) -> list[dict]:
        return self.list_metadata("CustomLabel")

    def query(self, soql: str) -> list[dict]:
        self._rate_limit()
        all_records = []
        resp = self._get("/query", params={"q": soql})
        all_records.extend(resp.get("records", []))
        while not resp.get("done", True) and resp.get("nextRecordsUrl"):
            self._rate_limit()
            url = self._instance_url + resp["nextRecordsUrl"]
            resp = self._session.get(url, headers=self._headers(), timeout=60).json()
            all_records.extend(resp.get("records", []))
        return all_records

    def describe_object(self, object_name: str) -> dict:
        cache_key = f"describe_{object_name}"
        if cached := self._from_cache(cache_key):
            return cached
        result = self._get(f"/sobjects/{object_name}/describe/")
        self._to_cache(cache_key, result)
        return result

    def list_all_objects(self) -> list[dict]:
        cache_key = "all_objects"
        if cached := self._from_cache(cache_key):
            return cached
        result = self._get("/sobjects/")
        objects = result.get("sobjects", [])
        self._to_cache(cache_key, objects)
        return objects
