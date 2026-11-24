# ibfupdater

The Intelligent Background File Updater downloads a file, replaces it only
when its contents change, and optionally keeps previous versions. HTTP cache
validation with ETag or Last-Modified avoids unnecessary downloads.

## Usage

```sh
ibfupdater --quiet --logger --enable-caching --keep 3 \
  --output /var/lib/blocklists/example.dat https://example.org/list.dat
```

The output directory must already exist and be writable. Run as the account
that should own the file; root is not required. The output must be a regular
file or a new filename. Symlinks, directories and other special files are
rejected, as are filenames ending in `.`, `ibfu-meta` or `ibfu-lock`.

Requirements: Bash 4.2 or newer, curl, awk with `fflush`, util-linux `flock`,
GNU coreutils and `cmp`. `logger` is needed for `--logger`. Supported download
schemes are HTTP, HTTPS, FTP, FTPS, SFTP, SCP and file, subject to curl's compiled
protocol support. A URL without a scheme uses HTTP. Conditional caching is
available for HTTP/HTTPS only. URL globbing is disabled, so brackets and braces
identify one literal URL. Redirects to local `file:` URLs are rejected.

## Downloads, caching and timestamps

- A complete HTTP 200 response can replace the output. HTTP 304 is accepted
  only after a conditional request for a matching local file. Partial content,
  other HTTP statuses and curl failures leave the active file unchanged.
- `--enable-caching` stores metadata in `.<filename>.ibfu-meta`, with mode
  `0600`. Strong and weak ETags are supported. `--disable-etag` selects the
  Last-Modified fallback when available.
- Only validators from the final response are saved; redirect and proxy
  headers are discarded. Metadata is refreshed even when a downloaded body
  is identical or a 304 changes the validators.
- Metadata is associated with the URL, user agent, additional curl arguments
  and local file identity, size and timestamps. Changed requests or locally
  edited files trigger a full download. This is cache validation, not an
  integrity or authenticity check. Changes to external credential sources
  are not detected; use `--force` when changing those resources.
- Old raw-header metadata is ignored and replaced after one unconditional
  download. No manual migration is necessary.
- `--force` bypasses conditional requests. It still compares downloaded bytes
  and preserves the active file and backups when the content is unchanged.
- Remote timestamps are used by default. `--disable-remote-time` uses the
  download time instead. Identical content retains the existing local file's
  timestamp and inode. Empty downloads are valid files.

curl's normal certificate verification remains enabled. Defaults are a
60-second connection timeout, a 60-second transfer timeout and two retries;
the total runtime can exceed 60 seconds when retries occur.

## Publication, backups and overlapping runs

Downloads and backup copies are staged in a private `.ibfu.<random>` directory
inside the output directory. This keeps publication on the same filesystem:
the active file is replaced by an atomic rename, with no interval where its
name is missing. Existing ownership and permission bits are preserved; new
files use the invoking account and its umask. Other inode attributes, such as
extended attributes, are not explicitly copied.

`--keep N` retains up to N previous, different versions as `<filename>.1`,
`<filename>.2`, and so on. `.1` is the most recent previous version. Gaps are
compacted and surplus backups are removed on the next changed download.
Without `--keep`, existing backups are left alone. Positive integers up to
2,147,483,647 are accepted; disk space is the practical limit. Canonical numeric
suffixes are reserved for backups; do not use them for unrelated files or
another updater's output.

Each invocation holds an exclusive lock on `.<filename>.ibfu-lock` throughout
the download and publication. Runs for the same output wait for one another;
different outputs can proceed independently. Parent-directory symlink aliases
share the same lock. The lock file intentionally remains after exit. Removing
it while another invocation holds or waits for it defeats the locking.

The active file, backup chain and metadata are **not one transaction**. An
error after publishing the active file reports that the new data is already
installed. Staged backups are retained for recovery if finalization fails.
Old cache metadata is invalidated before publication, so a later invocation
cannot validate new data using the previous file's validators.

Normal failures and handled signals clean up temporary files. `SIGKILL`, a
crash or power loss can leave temporary directories or incomplete backup
rotation behind. There is no durability guarantee against power loss.
`--keep-working-dir` preserves temporary files for inspection. The destination
directory should be controlled by trusted users; other writers must cooperate
with the updater's locking and publication scheme.

## Additional curl options

```sh
ibfupdater --curl-options '--user "username:password with spaces" --retry 4' \
  --output ./example.dat https://example.org/list.dat
```

`--curl-options` accepts a single string. Whitespace separates arguments;
single and double quotes group them. Outside single quotes, a backslash escapes
the next character. There is no variable, command or wildcard expansion.
Long options accept `--option=value` or `--option value`; short options require
a separate argument. Repeating `--curl-options` replaces the previous list.

Supported options without arguments:

```text
-4 -6 --ipv4 --ipv6 -k --insecure --http1.0 --http1.1 --http2 --http3
--basic --digest --anyauth --negotiate --ntlm -n --netrc --netrc-optional
--proxy-basic --proxy-digest --proxy-anyauth --retry-connrefused
```

Supported options taking one argument:

```text
-u --user -U --proxy-user -x --proxy --noproxy --cacert --capath
--cert --key --pass --netrc-file -b --cookie -H --header --referer
--connect-timeout -m --max-time --retry --retry-delay --retry-max-time
--max-redirs --max-filesize --limit-rate --speed-limit --speed-time
--interface --resolve --connect-to --tls-max
```

curl must support the selected options. `--user-agent` is an ibfupdater option.
Output controls, extra URLs, uploads, curl configuration files, abbreviations
and combined short options are rejected. Conditional and Range headers,
header files (`@file`) and multiline header values are also rejected.
The default `.curlrc` is disabled so it cannot add transfers or redirect output.
These checks are stricter than the old incomplete option filter.

For option semantics, see the [curl manual](https://curl.se/docs/manpage.html).

## Diagnostics and development

`--quiet` suppresses normal output; errors still go to stderr. `--logger` sends
success summaries and failure details to syslog. `--verbose` explains cache
decisions, transfer status, content comparison, backup rotation, publication
and cleanup, including why a conditional request or replacement is skipped.
`--debug` enables Bash tracing, which can reveal URLs and credentials.

Exit statuses: `0` success (including unchanged content), `1` general failure,
`2` curl or response failure, `64` invalid arguments, `74` file/I/O failure,
and `78` invalid configuration. Handled signals use `128 + signal number`.
A nonzero exit after publication can mean that the new file is already active.

```sh
bash -n ibfupdater
shellcheck ibfupdater
python3 tests/integration.py -v
```

The Python standard-library suite runs real Bash and curl against a temporary
HTTP server bound to loopback. Tests cover caching, redirects, truncated
responses, backup rotation, concurrent updates, signals and injected I/O
failures. Root and external servers are not required. Set `IBFU_TEST_BASH` to
test with another Bash executable. Temporary subprocess groups are cleaned up
on timeout. Version bumps and release tags are separate release steps.
