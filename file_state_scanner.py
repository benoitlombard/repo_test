# gui/model/file_state_scanner.py

import os
import subprocess
from pathlib import Path
from pathlib import PurePosixPath

class FileStateScanner:
    """
    Scans file states: locked, readonly, changed.
    Determines LFS mode via presence of .git/locks-wt-path.

    Public entry point is :func:`scan_directory_state`, which performs a
    *batched* scan for a whole directory (one git status + one git lfs locks /
    one Python no-LFS scan + per-file os.access for readonly), instead of
    looping one git call per file.
    """

    def __init__(self, bash_exe: str):
        """
        Args:
            bash_exe: Path to bash executable for running git commands.
        """
        self.bash_exe = bash_exe

    def scan_directory_state(self, repo_path: str, rel_dir: str, file_names: list[str]) -> dict:
        """
        Scan states for every file of a directory in a single batched pass.

        - 'changed' : one ``git status --porcelain -- "<rel_dir>"`` call,
          executed with cwd = repo_path (no ``cd`` into a subdirectory);
          the output is parsed to build a mapping of repo-relative paths
          to a ``changed`` boolean, then restricted to ``file_names``.
        - 'locked' (LFS mode) : one ``git lfs locks`` call (without
          ``--path``), executed with cwd = repo_path; ``git lfs locks``
          always lists the whole repo so there is no benefit in filtering;
          the tab-separated output is parsed to collect the basenames of
          every locked file, then intersected with ``file_names`` by
          basename.
        - 'locked' (no-LFS mode) : one Python-pure scan of the
          ``{LOCK_WORKTREE_PATH}/.file-locks`` directory, collecting all
          ``{basename}.lockedby.*`` markers in one pass (no bash call).
        - 'readonly' : ``os.access(os.W_OK)`` per file (kept per-file as
          it is a single syscall and cheap).

        Args:
            repo_path: Absolute path to git repository root.
            rel_dir:   Directory being scanned, relative to ``repo_path``.
                       ``""`` (empty string) means the repository root.
            file_names: List of basenames of the files in ``rel_dir``.

        Returns:
            ``{rel_path: {'locked': ..., 'readonly': ..., 'changed': ...}}``
            where ``rel_path`` is repo-root-relative (i.e. ``rel_dir/name``
            if ``rel_dir`` is non-empty, else just ``name``).

            Each value uses the same tri-state strings as the legacy
            per-file implementation, so the GUI (file_table) keeps working
            unchanged:
                - ``'●'`` = active (locked, readonly, or modified)
                - ``'○'`` = inactive (not locked, writable, or clean)
                - ``'-'`` = unknown (error or unavailable)
        """
        result = {}
        if not repo_path or not file_names:
            return result

        # Build the repo-root-relative paths once. Forward-slashed so they
        # match what `git status --porcelain` prints.
        if rel_dir:
            rel_paths = [
                os.path.join(rel_dir, name).replace(os.sep, '/')
                for name in file_names
            ]
        else:
            rel_paths = list(file_names)

        # ---- changed : ONE git status --porcelain call (cwd=repo_path) ----
        changed_set = self._get_changed_set(repo_path, rel_dir)

        # ---- locked : ONE git lfs locks call OR a single Python scan ----
        if self._is_no_lfs_mode(repo_path):
            locked_by_map = self._get_locked_by_map_no_lfs(repo_path)
        else:
            locked_by_map = self._get_locked_by_map_lfs(repo_path)

        # ---- readonly : per-file os.access (cheap, no subprocess) ----
        for rel_path, file_name in zip(rel_paths, file_names):
            abs_path = os.path.join(repo_path, rel_path)
            readonly = self._check_readonly(abs_path)
            locked_owner = locked_by_map.get(file_name)
            locked = '●' if locked_owner is not None else '○'
            changed = '●' if rel_path in changed_set else '○'
            result[rel_path] = {
                'locked': locked,
                'readonly': readonly,
                'changed': changed,
                'locked_by': locked_owner if locked_owner is not None else '-',
            }

        return result

    def _is_no_lfs_mode(self, repo_path: str) -> bool:
        """
        Detect no-LFS mode by checking for .git/locks-wt-path file.

        Args:
            repo_path: Absolute path to git repository root.

        Returns:
            True if no-LFS mode (file exists), False otherwise.
        """
        locks_wt_path_file = os.path.join(repo_path, '.git', 'locks-wt-path')
        
        return os.path.isfile(locks_wt_path_file)

    def _get_changed_set(self, repo_path: str, rel_dir: str) -> set:
        """
        Run ``git status --porcelain -- "<rel_dir>"`` with cwd=repo_path and
        return the set of repo-relative paths that are changed.

        Handles the usual porcelain quirks: quoted paths with escapes,
        ``old -> new`` rename suffixes.
        """
        try:
            target = rel_dir.replace(os.sep, '/') if rel_dir else '.'
            result = subprocess.run(
                [self.bash_exe, '-c', f'git status --porcelain -- "{target}"'],
                capture_output=True,
                text=True,
                timeout=100,
                cwd=repo_path,
            )
            if result.returncode != 0:
                return set()

            changed = set()
            for line in result.stdout.splitlines():
                # porcelain format: XY<space>path   (XY = 2 status chars)
                if len(line) < 4:
                    continue
                path_part = line[3:]
                if path_part.startswith('"') and path_part.endswith('"'):
                    path_part = path_part[1:-1]
                    path_part = path_part.replace('\\"', '"').replace('\\\\', '\\')
                # Renames print "old -> new"; we only care about the new path.
                if ' -> ' in path_part:
                    path_part = path_part.split(' -> ', 1)[1]
                changed.add(path_part)
            return changed
        except Exception:
            return set()

    def _get_locked_by_map_lfs(self, repo_path: str) -> dict:
        """
        Run ``git lfs locks`` (no ``--path``, cwd=repo_path) and return the
        set of basenames currently locked anywhere in the repo.
        """
        try:
            result = subprocess.run(
                [self.bash_exe, '-c', 'git lfs locks'],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=repo_path,
            )
            if result.returncode != 0:
                return {}

            locked_by = {}
            for line in result.stdout.splitlines():
                if not line:
                    continue
                # git lfs locks prints tab-separated rows: ID<tab>Owner<tab>Path
                parts = line.split('\t')
                if len(parts) >= 3:
                    path = parts[2].strip()
                    owner = parts[1]
                    if owner.endswith('\n'):
                        owner = owner[:-1]
                    if path:
                        locked_by[PurePosixPath(path).name] = owner
            return locked_by
        except Exception:
            return {}

    def _get_locked_by_map_no_lfs(self, repo_path: str) -> dict:
        """
        Python-pure scan of ``{LOCK_WORKTREE_PATH}/.file-locks/`` collecting
        every basename that has a ``{basename}.lockedby.*`` marker. Called
        once per ``scan_directory_state`` instead of once per file.
        """
        try:
            locks_wt_path_file = os.path.join(repo_path, '.git', 'locks-wt-path')
            if not os.path.isfile(locks_wt_path_file):
                return {}

            with open(locks_wt_path_file, 'r') as f:
                locks_wt = f.read().strip()

            locks_wt_win = self._msys_to_windows_path(locks_wt)
            if not locks_wt_win or not os.path.isdir(locks_wt_win):
                return {}

            marker_dir = os.path.join(locks_wt_win, '.file-locks')
            if not os.path.isdir(marker_dir):
                return {}

            locked_by = {}
            for item in os.listdir(marker_dir):
                if '.lockedby.' in item:
                    basename, owner = item.split('.lockedby.', 1)
                    locked_by[basename] = owner
            return locked_by
        except Exception:
            return {}

    def _check_readonly(self, abs_path: str) -> str:
        """
        Check if file is readonly.

        Args:
            abs_path: Absolute path to file.

        Returns:
            '●' if readonly, '○' if writable, '-' if file does not exist.
        """
        try:
            if not os.path.exists(abs_path):
                return '-'
            # os.access(path, os.W_OK) returns True if writable
            # So readonly = not writable
            is_writable = os.access(abs_path, os.W_OK)
            return '○' if is_writable else '●'
        except Exception:
            return '-'

    def _msys_to_windows_path(self, msys_path: str) -> str:
        """
        Convert an MSYS/Git Bash path (e.g. /tmp/xxx) to a Windows path.

        Args:
            msys_path: Path as seen by Git Bash (e.g. '/tmp/foo').

        Returns:
            Windows path (e.g. 'C:\\Users\\...\\Temp\\foo'), or empty string on failure.
        """
        try:
            result = subprocess.run(
                [self.bash_exe, '-c', f'cygpath -w "{msys_path}"'],
                capture_output=True,
                timeout=5,
                text=True
            )
            return result.stdout.strip()
        except Exception:
            return ''

