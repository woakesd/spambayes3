# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Spambayes3 is a Python 3 port of the original SpamBayes spam filter (originally Python 2, based on spambayes-1.1a6 with updates from 1.1b3). It uses Bayesian probability to classify email as spam, ham, or unsure. The primary maintained entry points are `sb_filter.py` (procmail-style filter) and `sb_mboxtrain.py` (mbox training).

## Environment & Package Management

This project uses `uv` for dependency and virtual environment management.

```sh
# Install dependencies and package in editable mode
uv pip install -e .

# Build distributable packages
uv build

# Run any command in the project environment
uv run <command>
```

The venv is at `.venv/`. Python 3.14 is used (see `.venv/pyvenv.cfg`). Dependencies: `lockfile>=0.6`, `py3dns>=2.0`.

**Note:** `safepickle.py` imports `filelock` (not `lockfile`). If you see `ModuleNotFoundError: No module named 'filelock'`, install it with `uv pip install filelock`.

## Running Tests

Tests live in `spambayes/test/`. Many test files depend on `sb_test_support.fix_sys_path()`, which tries to import `sb_server` from `spambayes/scripts/` — tests that use this will fail if that path isn't on `sys.path`.

Run a specific test file directly:
```sh
uv run python spambayes/test/test_basic_import.py
```

Run a single test class or method:
```sh
uv run python -m unittest spambayes.test.test_Corpus.CorpusTest.testSomeMethod
```

**Known permanently broken tests** (require network services or Python 2 syntax): `test_sb_imapfilter`, `test_smtpproxy`, `test_programs` (server tests), and `test_storage.DBStorageTestCase`. These are documented in `failing-unit-tests.txt` (file reflects the Python 2 era failures, not current state).

Working tests: `test_basic_import`, `test_storage` (Pickle/CDB/ZODB variants).

## Core Architecture

The classification pipeline flows through these modules:

1. **`mboxutils.py`** — Parses raw email input into `email.message.EmailMessage` objects. Entry point for both filter and training paths.
2. **`tokenizer.py`** — Splits email messages into tokens (words/features). Handles MIME parts, HTML stripping, URL extraction, header analysis. Uses `email.message.EmailMessage` (not the legacy `email.message.Message`).
3. **`classifier.py`** — Core Bayesian classifier implementing Gary Robinson's chi-squared combining scheme. `spamprob()` scores a token stream; `learn()`/`unlearn()` update word probabilities.
4. **`storage.py`** — Persistence layer. `PickledClassifier` (pickle), `DBDictClassifier` (shelve/dbm), `ZODBClassifier`, `CDBClassifier`. All are subclasses of `classifier.Classifier` with load/store added.
5. **`hammie.py`** — Ties the above together. `Hammie` wraps a storage-backed classifier with `filter()`, `train()`, `untrain()` methods. `hammie.open()` is the main factory.

**Configuration** is global via `Options.options` (a singleton loaded from `bayescustomize.ini` or `~/.spambayesrc` or the `BAYESCUSTOMIZE` env var). `OptionsClass.py` defines the option schema; `Options.py` instantiates the global object.

**Scripts** (`spambayes/scripts/`) are the user-facing entry points:
- `sb_filter.py` — procmail filter; reads from stdin, writes scored mail to stdout
- `sb_mboxtrain.py` — batch-trains from Unix mbox files (`-g` for ham, `-s` for spam)
- `sb_server.py` — POP3 proxy with web UI (port 8880)
- `sb_imapfilter.py` — IMAP folder filter/trainer

**Web UI** is built on `Dibbler.py` (custom async HTTP server based on `asyncore`). `UserInterface.py` provides the base; `ProxyUI.py`, `ImapUI.py`, `ServerUI.py` extend it. Templates use `PyMeldLite.py`.

## Python 2→3 Migration Notes

This is an ongoing port. Key changes already made (see `2TO3MIGRATION.md`):
- `email.Message` → `email.message.EmailMessage` with `email.policy.default`
- `mailbox.PortableUnixMailbox` → `mailbox.mbox`
- `bsddb` → `berkeleydb` (for DBM storage)
- `filelock` replaces `lockfile` in `safepickle.py`
- `tokenizer.py` reworked to handle bytes/str boundary in email payloads

Modules **not yet fully converted** to Python 3: many test files still contain Python 2 `print` statements and other syntax. The Outlook2000 plugin and several proxy scripts are untested.

When modifying code, prefer `email.message.EmailMessage` over the legacy `email.message.Message`. Use `get_content()` instead of `get_payload()` on email parts where possible.
