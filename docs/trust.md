# Signed results anyone can check

Stage 4, the exam, signs every report with an HMAC key that lives in the workspace
(`signing.key`). Only a host holding that key can check an HMAC signature, so a package
signed with the HMAC alone refuses to load anywhere else. Reports, licenses and export
manifests can also carry an Ed25519 signature next to the HMAC, which anyone can check
offline with the public key alone.

The code is in `architect/trust.py` (keys, trust list, signatures),
`architect/_authority.py` (report signing), `architect/forge_modes/export.py`
(packages) and `architect/verdict.py` (`architect verify`).

## What gets signed

On a host that holds an Ed25519 private key:

- every Stage 4 report gets an `ed25519` field next to its HMAC `signature`. It covers
  every other field of the report, the HMAC included. A license is such a report inside
  an artifact folder, bound to the code by `artifact_digest` and `spec_hash`;
- every export manifest (`MANIFEST.json`) gets an `ed25519` field over the canonical
  manifest. The manifest lists the sha256 of every other file in the package;
- a published report folder gets a `SIGNED.json` listing the sha256 of every file in
  it, signed as a whole (`architect keys sign`).

A signature names its key by fingerprint: the sha256 of the raw 32-byte public key, as
64 hex digits. It never carries the key itself. Each signature also names what it
signs (`report`, `manifest` or `published`), so one made for a report never checks as
a manifest.

A host without an Ed25519 key signs exactly as before: HMAC only. Nothing in its reports
or manifests changes, so every artifact signed with the HMAC alone keeps its bytes, its
tests and its old rules.

## The trust list

A signature counts only when its key is on this host's trust list. The list is local:

- by default it holds one key, the release key shipped in the package at
  `architect/keys/architect-ed25519.pub` (see [The release key](#the-release-key));
- a workspace can add keys in `trusted-keys.json`, next to its `signing.key`. The file
  `ARCHITECT_TRUST_FILE` names is used instead when that variable is set;
- with `"shipped": false` in that file, the shipped key is no longer trusted there;
- a trust file that cannot be read trusts nothing at all, and neither does a file that
  `ARCHITECT_TRUST_FILE` names but that does not exist (`trust_list_missing`). A typo in
  that variable never falls back to the shipped key.

```json
{
  "keys": [{"name": "bench", "public_key": "<base64 of the raw 32-byte key>"}],
  "shipped": true
}
```

`architect keys trust <file.pub> --name <name>` adds a key (PEM or base64) and
`architect keys list` shows what this workspace trusts; both print the trust list's
absolute path. A key that arrives inside a package, a report or a published folder is
never trusted on its own; the fingerprint there only says which trusted key to check with.

A key of small order (any of the 14 encodings of the eight points of order 1, 2, 4 or 8)
is refused as `key_invalid`, by `keys trust` and inside a trust file alike. Such a key
checks a constant signature that anyone can write without a private key, so trusting it
would let anyone sign as it.

Pi file tools are denied the private key and the trust file, so a coding model can
neither read the key nor add a key of its own. The checks compare paths without case
(APFS and NTFS, the macOS and Windows defaults, open a case variant as the same file) and
refuse 8.3 short names such as `TRUSTE~1.JSO`, in the Python guard and in the Pi
extension alike. The Pi extension also refuses any path that opens with `\\`: a UNC
share, a loopback address like `\\localhost\C$\...`, an extended-length prefix
(`\\?\C:\...`) or a device path all name the same tree as a drive path but keep their own
leading text, which the realpath comparison would miss. The conductor's `read_file`
refuses the keys by case variant and by file identity too, and its listings hide case
variants of their names.

## Which keys count

`architect verify`, `architect export verify`, `ExportedHarness` (so an exported
package's `verify()`, `run()` and its own tests) check files that may come from
anywhere, so they use configured keys only:

- the HMAC key counts only when `ARCHITECT_SIGNING_KEY_FILE` (or `--workspace`) names it;
- the trust list is the file `ARCHITECT_TRUST_FILE` names, else `trusted-keys.json`
  next to that HMAC key, else none: then only the key shipped with Architect is trusted;
- `./.architect` never counts on its own. A key or a trust list that arrives with a
  downloaded folder therefore cannot vouch for that folder;
- an HMAC key or a trust list that lies inside the folder being checked never vouches
  for it, even when a variable names it: that is `trust_in_target`. The "inside" test
  matches by file identity, not by path text, so pointing a variable or `--workspace` at
  the in-folder key through a UNC share, a loopback address, an extended-length prefix
  (`\\?\C:\...`) or a device path does not slip it past the check.

Compiling and signing are unchanged: a CLI compile without the variable still signs with
`.architect/signing.key` under the working directory. `forge integrate` names that
folder's keys for its own re-check, as a session does, so the package it just placed is
checked with the keys that signed it. To check a result later, name the keys, for example
`architect verify --workspace .architect <package>`.

## What the checks do

`check_package` (and so `ExportedHarness.load` and an exported package's `verify()`),
registry loads and `Runtime.run` accept a license when either this host's HMAC key
signed it or a trusted key's Ed25519 signature checks out. Then:

- a license that verifies by its Ed25519 signature needs a manifest signed by a trusted
  key too. Without one the package is refused with `unsigned`, and with one from an
  unknown key with `key_untrusted`;
- any changed byte in a signed package is `artifact_changed`: a file that differs from
  the signed manifest, an added or missing file, an edited manifest, an edited report,
  or code that no longer matches the signed report;
- a license signed only by another host's HMAC is still `license_unverifiable` here.

So a package built and exported on a host with an Ed25519 key runs on a clean machine
that holds only the public key, and refuses to load after a one-byte edit.

Every package carries a `.gitattributes` with `* -text`, so a checkout with
`core.autocrlf=true` keeps its files byte for byte as signed. The manifest lists only
plain relative names (no `..`, no absolute or drive paths, no hidden names other than
that `.gitattributes`); any other name makes the manifest unreadable. Files an operating
system writes into a folder you open (`.DS_Store`, `._` files, `Thumbs.db` and
`desktop.ini`) are never listed and never checked, so opening a package in the Finder or
Explorer does not break it.

The evidence label (`ed25519_signed` or `host_attested`) follows the signature that
verifies on the exporting host. After a key rotation, a license sealed by the old key is
exported as `host_attested`, and the package never names the old key.

The package's own tests skip the license tests only on `license_unverifiable`, where
nothing can check the license at all. A seal from a key the host does not trust fails
them. Since a copy stripped back to the HMAC would skip there, CI should also run
`architect verify --require-key <fingerprint> <package>` outside the package.

## architect verify

```
architect verify path/to/report.json
architect verify path/to/artifact-folder
architect verify path/to/exported_package
architect verify path/to/published-folder
architect verify --published receipts
```

It prints `verified` or `refused: <code>`, then a few plain ASCII lines: what was
checked, the exam result for a license, and which key vouched for it. Text from a signed
report or an artifact's config (failures, the model, the preset) is shown as printable
ASCII only, so it can carry no terminal escapes. When a key comes from a workspace trust
list rather than the shipped one, the last line names the list's absolute path. When an
HMAC key vouched, the line names that key's absolute path. It reads only. It never runs
package code, creates a key or touches the network. Add `--json` for a machine-readable
verdict and `--workspace <dir>` to use that workspace's keys and trust list. Without
`--workspace` or an `ARCHITECT_*` variable it trusts the shipped key alone and no HMAC
key (see [Which keys count](#which-keys-count)). The exit code is 0 only for `verified`.

`--require-key <fingerprint>` (the full 64 hex digits that `architect keys list` shows)
accepts only an Ed25519 signature by that key, which must be on the trust list. No HMAC
key vouches then, so a judge or a CI job can refuse a result whose signature was
stripped back to the host-only HMAC, or that another key signed:

```
architect verify --require-key <fingerprint> path/to/exported_package
```

`--published <name>` checks the folder `architect/published/<name>/` that ships inside
the installed package. The folder must have been signed under that name, so another
signed folder copied in its place is refused. `architect/published/receipts` holds the
receipt evaluation of 2026-10-05, signed with the release key: the evaluation plan, the
bench rows of the plain models, and the ladder build's outcome and exam reports. Files an
operating system writes into a folder you open (`.DS_Store`, `._` files, `Thumbs.db` and
`desktop.ini`) are never signed and never checked, and the verdict names any it skipped.

What a pass means: the bytes are the ones the key holder signed. It does not prove the
exam behind a report ran honestly. That still rests on the host that ran it, and the
key only tells you which host that was.

## The release key

The release key was made on 2026-10-05, on the bench host. Its public half ships in the
package at `architect/keys/architect-ed25519.pub`, so a fresh install trusts this key and
no other. Its fingerprint, the sha256 of the raw public key, is:

```
39762f2071b307da0db6ada328bc0743b228351fe2b78d890b50d09bf6cc30f8
```

`architect verify` names it by its first 16 digits, `39762f2071b307da`. To accept only a
signature by this key:

```
architect verify --require-key 39762f2071b307da0db6ada328bc0743b228351fe2b78d890b50d09bf6cc30f8 path/to/exported_package
```

The private key stays on the bench host, with one private offline backup (step 1 below),
and is never committed.
Tests make their own throwaway keys at run time, and no private key is ever stored in the
repository.

## On the bench host

The private key is `ed25519.key` in a workspace of its own, outside every repository
(`~/.architect-release` on the bench host), next to a trust list that names it. It was
made once, from the repository root, with:

```
architect keys generate --name "architect release" --workspace <workspace> --pub architect/keys/architect-ed25519.pub
```

That wrote `<workspace>/ed25519.key` (mode 0600 where the system has modes), added the
key to that workspace's trust list and wrote the public half into the package. The
command refuses to replace an existing private key or a public key file that already
holds a key.

1. Back up `ed25519.key` somewhere private and offline. Losing it means a new key, a
   new release and new signatures; leaking it means anyone can sign as Architect, so
   the key must then be replaced in the next release.
2. A build or an export signs with the key only when `ARCHITECT_ED25519_KEY_FILE` names
   it in the environment of the process that runs it:
   `ARCHITECT_ED25519_KEY_FILE=<workspace>/ed25519.key`. Bench cells, the `architect`
   agent and the desktop app each work in a workspace of their own that holds no copy
   of the key, so without the variable their reports and manifests are signed with the
   HMAC only. The test suite removes the variable, so no test build is ever signed with
   the release key.
3. After any change to `architect/published/receipts/`, sign the folder again and check
   it:

   ```
   architect keys sign architect/published/receipts --workspace <workspace>
   architect verify --published receipts
   ```

   Commit the folder with its `SIGNED.json`. `.gitattributes` keeps these files byte
   for byte as signed on every platform.
4. Before a commit, check `git status`: neither the private key nor the workspace's
   trust list may appear in it.

## Codes

| Code | Meaning |
|---|---|
| `artifact_changed` | Bytes differ from what a trusted key signed. |
| `key_untrusted` | Signed by a key this workspace does not trust. |
| `unsigned` | No signature this host trusts covers these files. |
| `license_unverifiable` | No Ed25519 signature, and no HMAC key here may check it. |
| `trust_list_invalid` | The trust file cannot be read, so no key is trusted. |
| `trust_list_missing` | `ARCHITECT_TRUST_FILE` names a file that does not exist, so no key is trusted. |
| `trust_in_target` | The HMAC key or the trust list lies inside the folder being checked. |
| `published_unknown` | No published folder by that name ships with this install. |
| `published_invalid` | The folder holds a link, a special file or an unusual file name. |
| `verify_target_unknown` | Not a report, artifact, package or published folder. |
| `key_exists` | A key file is already there and was not replaced. |
| `key_missing` | This host holds no Ed25519 private key (for `keys sign`). |
| `key_invalid` | A key file is not a usable Ed25519 key. |
