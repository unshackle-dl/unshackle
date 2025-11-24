# Service Integration & Authentication Configuration

This document covers service-specific configuration, authentication, and metadata integration options.

## services (dict)

Configuration data for each Service. The Service will have the data within this section merged into the `config.yaml`
before provided to the Service class.

Think of this config to be used for more sensitive configuration data, like user or device-specific API keys, IDs,
device attributes, and so on. A `config.yaml` file is typically shared and not meant to be modified, so use this for
any sensitive configuration data.

The Key is the Service Tag, but can take any arbitrary form for its value. It's expected to begin as either a list or
a dictionary.

For example,

```yaml
NOW:
  client:
    auth_scheme: MESSO
    # ... more sensitive data
```

---

## credentials (dict[str, str|list|dict])

Specify login credentials to use for each Service, and optionally per-profile.

For example,

```yaml
ALL4: jane@gmail.com:LoremIpsum100 # directly
AMZN: # or per-profile, optionally with a default
  default: jane@example.tld:LoremIpsum99 # <-- used by default if -p/--profile is not used
  james: james@gmail.com:TheFriend97
  john: john@example.tld:LoremIpsum98
NF: # the `default` key is not necessary, but no credential will be used by default
  john: john@gmail.com:TheGuyWhoPaysForTheNetflix69420
```

The value should be in string form, i.e. `john@gmail.com:password123` or `john:password123`.
Any arbitrary values can be used on the left (username/password/phone) and right (password/secret).
You can also specify these in list form, i.e., `["john@gmail.com", ":PasswordWithAColon"]`.

If you specify multiple credentials with keys like the `AMZN` and `NF` example above, then you should
use a `default` key or no credential will be loaded automatically unless you use `-p/--profile`. You
do not have to use a `default` key at all.

Please be aware that this information is sensitive and to keep it safe. Do not share your config.

---

## tmdb_api_key (str)

API key for The Movie Database (TMDB). This is used for tagging downloaded files with TMDB,
IMDB and TVDB identifiers. Leave empty to disable automatic lookups.

To obtain a TMDB API key:

1. Create an account at <https://www.themoviedb.org/>
2. Go to <https://www.themoviedb.org/settings/api> to register for API access
3. Fill out the API application form with your project details
4. Once approved, you'll receive your API key

For example,

```yaml
tmdb_api_key: cf66bf18956kca5311ada3bebb84eb9a # Not a real key
```

**Note**: Keep your API key secure and do not share it publicly. This key is used by the core/utils/tags.py module to fetch metadata from TMDB for proper file tagging.

---

## simkl_client_id

**Description:** SIMKL API integration for tracking

**Note:** This configuration option was not found in the original CONFIG.md documentation. If this feature exists, please refer to the service documentation or contact the maintainers for configuration details.

---

## title_cache_enabled (bool)

Enable/disable caching of title metadata to reduce redundant API calls. Default: `true`.

---

## title_cache_time (int)

Cache duration in seconds for title metadata. Default: `1800` (30 minutes).

---

## title_cache_max_retention (int)

Maximum retention time in seconds for serving slightly stale cached title metadata when API calls fail.
Default: `86400` (24 hours). Effective retention is `min(title_cache_time + grace, title_cache_max_retention)`.

---
