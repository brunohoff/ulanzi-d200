# Contributing to Ulanzi D200 Stream Controller

First off, thanks for taking the time to contribute! 🎉 

The following is a set of guidelines for contributing to this project. These are mostly guidelines, not rules. Use your best judgment, and feel free to propose changes to this document in a pull request.

## Code of Conduct
Please ensure that your interactions in issues and pull requests are respectful and constructive. 

## How Can I Contribute?

### Reporting Bugs
- **Ensure the bug was not already reported** by searching on GitHub under Issues.
- If you're unable to find an open issue addressing the problem, open a new one. Be sure to include a title and clear description, as much relevant information as possible, and a code sample or an executable test case demonstrating the expected behavior that is not occurring.

### Suggesting Enhancements
- Open a new issue with a clear title and a detailed description of the proposed enhancement.
- Explain why this enhancement would be useful to most users.

### Pull Requests
1. Fork the repo and create your branch from `main`.
2. If you've added code that should be tested, add tests to `test_d200.py`.
3. Ensure the test suite passes (`python3 test_d200.py`).
4. Keep your PRs focused and small.
5. Update the documentation (`README.md`) if you introduce new features.

## Setup for Development
1. Clone the repository.
2. We recommend using a virtual environment: `python3 -m venv .venv && source .venv/bin/activate`
3. Install dependencies: `pip install -r requirements.txt`
4. Copy `config.example.json` to `config.json` and adjust as needed.
5. Run tests: `python3 test_d200.py`

Thanks again for your interest in making this project better!
