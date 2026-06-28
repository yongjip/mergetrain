# Release checklist

1. Run tests.

   ```sh
   PYTHONPATH=src python -m unittest discover -s tests
   ```

2. Verify CLI contract.

   ```sh
   PYTHONPATH=src python -m mergetrain agent-contract --json
   ```

3. Build package.

   ```sh
   python -m build
   ```

4. Install wheel in a clean environment.

   ```sh
   python -m pip install dist/*.whl
   mergetrain --version
   ```

5. Tag release.

   ```sh
   git tag v0.1.0
   git push origin v0.1.0
   ```

6. Release note highlights.

   - SQLite-backed local deploy queue.
   - Runner lock with PID liveness.
   - Git worktree merge trains.
   - Configurable gates and atomic push refs.
   - Auto-only daemon.
   - JSON doctor/status/agent-contract.
