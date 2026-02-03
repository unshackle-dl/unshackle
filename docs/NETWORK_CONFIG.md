# Network & Proxy Configuration

This document covers network and proxy configuration options for bypassing geofencing and managing connections.

## proxy_providers (dict)

Enable external proxy provider services. These proxies will be used automatically where needed as defined by the
Service's GEOFENCE class property, but can also be explicitly used with `--proxy`. You can specify which provider
to use by prefixing it with the provider key name, e.g., `--proxy basic:de` or `--proxy nordvpn:de`. Some providers
support specific query formats for selecting a country/server.

### basic (dict[str, str|list])

Define a mapping of country to proxy to use where required.
The keys are region Alpha 2 Country Codes. Alpha 2 Country Codes are `[a-z]{2}` codes, e.g., `us`, `gb`, and `jp`.
Don't get this mixed up with language codes like `en` vs. `gb`, or `ja` vs. `jp`.

Do note that each key's value can be a list of strings, or a string. For example,

```yaml
us:
  - "http://john%40email.tld:password123@proxy-us.domain.tld:8080"
  - "http://jane%40email.tld:password456@proxy-us.domain2.tld:8080"
de: "https://127.0.0.1:8080"
```

Note that if multiple proxies are defined for a region, then by default one will be randomly chosen.
You can choose a specific one by specifying it's number, e.g., `--proxy basic:us2` will choose the
second proxy of the US list.

### nordvpn (dict)

Set your NordVPN Service credentials with `username` and `password` keys to automate the use of NordVPN as a Proxy
system where required.

You can also specify specific servers to use per-region with the `server_map` key.
Sometimes a specific server works best for a service than others, so hard-coding one for a day or two helps.

For example,

```yaml
username: zxqsR7C5CyGwmGb6KSvk8qsZ # example of the login format
password: wXVHmht22hhRKUEQ32PQVjCZ
server_map:
  us: 12 # force US server #12 for US proxies
```

The username and password should NOT be your normal NordVPN Account Credentials.
They should be the `Service credentials` which can be found on your Nord Account Dashboard.

Once set, you can also specifically opt in to use a NordVPN proxy by specifying `--proxy=gb` or such.
You can even set a specific server number this way, e.g., `--proxy=gb2366`.

Note that `gb` is used instead of `uk` to be more consistent across regional systems.

### surfsharkvpn (dict)

Enable Surfshark VPN proxy service using Surfshark Service credentials (not your login password).
You may pin specific server IDs per region using `server_map`.

```yaml
username: your_surfshark_service_username # https://my.surfshark.com/vpn/manual-setup/main/openvpn
password: your_surfshark_service_password # service credentials, not account password
server_map:
  us: 3844 # force US server #3844
  gb: 2697 # force GB server #2697
  au: 4621 # force AU server #4621
```

### hola (dict)

Enable Hola VPN proxy service. Requires the `hola-proxy` binary to be installed and available in your PATH.

```yaml
proxy_providers:
  hola: {}
```

Once configured, use `--proxy hola:us` or similar to connect through Hola.

### windscribevpn (dict)

Enable Windscribe VPN proxy service using static OpenVPN service credentials.

Use the service credentials from https://windscribe.com/getconfig/openvpn (not your account login credentials).

```yaml
proxy_providers:
  windscribevpn:
    username: openvpn_username  # From https://windscribe.com/getconfig/openvpn
    password: openvpn_password  # Service credentials, NOT your account password
```

#### Server Mapping

You can optionally pin specific servers using `server_map`:

```yaml
proxy_providers:
  windscribevpn:
    username: openvpn_username
    password: openvpn_password
    server_map:
      us: us-central-096.totallyacdn.com  # Force specific US server
      gb: uk-london-001.totallyacdn.com   # Force specific UK server
```

Once configured, use `--proxy windscribe:us` or `--proxy windscribe:gb` etc. to connect through Windscribe.

### Legacy nordvpn Configuration

**Legacy configuration. Use `proxy_providers.nordvpn` instead.**

Set your NordVPN Service credentials with `username` and `password` keys to automate the use of NordVPN as a Proxy
system where required.

You can also specify specific servers to use per-region with the `server_map` key.
Sometimes a specific server works best for a service than others, so hard-coding one for a day or two helps.

For example,

```yaml
nordvpn:
  username: zxqsR7C5CyGwmGb6KSvk8qsZ # example of the login format
  password: wXVHmht22hhRKUEQ32PQVjCZ
  server_map:
    us: 12 # force US server #12 for US proxies
```

The username and password should NOT be your normal NordVPN Account Credentials.
They should be the `Service credentials` which can be found on your Nord Account Dashboard.

Note that `gb` is used instead of `uk` to be more consistent across regional systems.

---

## headers (dict)

Case-Insensitive dictionary of headers that all Services begin their Request Session state with.
All requests will use these unless changed explicitly or implicitly via a Server response.
These should be sane defaults and anything that would only be useful for some Services should not
be put here.

Avoid headers like 'Accept-Encoding' as that would be a compatibility header that Python-requests will
set for you.

I recommend using,

```yaml
Accept-Language: "en-US,en;q=0.8"
User-Agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/77.0.3865.75 Safari/537.36"
```

---
