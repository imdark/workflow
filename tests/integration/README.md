# Browser Integration Tests

This directory contains integration tests for the browser CLI commands.

## Structure

```
tests/integration/
├── conftest.py                    # Shared fixtures for test isolation
├── README.md                      # This file
├── test_health.py                # Health endpoint tests
├── test_open.py                   # URL open tests
├── test_screenshot.py             # Screenshot tests
├── test_eval.py                   # JavaScript evaluation tests
├── test_snapshot.py               # Page snapshot tests
├── test_click.py                  # Click tests
├── test_fill.py                   # Fill/input tests
├── test_wait.py                    # Wait tests
├── test_pages.py                  # Pages listing tests
└── test_browser_destructive.py    # Destructive tests (may affect production)
```

## Running Tests

### Non-destructive tests only (safe to run anytime):
```bash
make test
# or
pytest tests/integration/ -v --ignore=tests/integration/test_browser_destructive.py
```

### Run specific test file:
```bash
pytest tests/integration/test_open.py -v
pytest tests/integration/test_screenshot.py -v
```

### Destructive tests (may affect other team members):
```bash
pytest tests/integration/test_browser_destructive.py -v
```

## Test Isolation

Each test:
- Uses a unique daemon port to avoid conflicts
- Creates isolated state directories  
- Cleans up after itself
- Can run in parallel with other tests

## Fixtures

- `browser_daemon` - Starts a fresh daemon for each test
- `daemon_url` - Provides the daemon URL
- `clean_port` - Ensures port is clean before/after test
- `test_state_dir` - Isolated state directory
