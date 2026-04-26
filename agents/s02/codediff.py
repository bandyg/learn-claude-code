import subprocess
from pathlib import Path

OUTPUT_DIR = "diff"

# 过滤规则
IGNORE_EXTENSIONS = {".lock", ".md", ".png", ".jpg", ".jpeg", ".gif"}
IGNORE_DIR_KEYWORDS = {"dist/", "build/", "node_modules/", ".git/"}

# 限制 diff 大小（防 LLM 爆）
MAX_DIFF_LENGTH = 4000


def run_cmd(cmd: list[str], cwd: str | None = None) -> str:
    result = subprocess.run(
        cmd,
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=cwd,
    )
    if result.returncode != 0:
        pretty_cmd = " ".join(cmd)
        raise Exception(f"Command failed: {pretty_cmd}\n{result.stderr}")
    return result.stdout


def get_repo_root() -> str:
    return run_cmd(["git", "rev-parse", "--show-toplevel"]).strip()


def should_ignore(file_path: str) -> bool:
    # 忽略扩展名
    for ext in IGNORE_EXTENSIONS:
        if file_path.endswith(ext):
            return True

    # 忽略目录
    for keyword in IGNORE_DIR_KEYWORDS:
        if keyword in file_path:
            return True

    return False


def get_changed_files(commit, repo_root):
    cmd = ["git", "show", "--name-only", "--pretty=format:", commit]
    output = run_cmd(cmd, cwd=repo_root)

    files = []
    for f in output.split("\n"):
        f = f.strip()
        if not f:
            continue

        if should_ignore(f):
            print(f"Skipped (ignored): {f}")
            continue

        files.append(f)

    return files


def get_file_diff(commit, file_path, repo_root):
    cmd = [
        "git",
        "show",
        "-U3",
        "--no-color",
        "--pretty=format:",
        commit,
        "--",
        file_path,
    ]
    diff = run_cmd(cmd, cwd=repo_root)

    # 过滤 binary
    if "Binary files differ" in diff:
        print(f"Skipped (binary): {file_path}")
        return ""

    # 限制大小
    if len(diff) > MAX_DIFF_LENGTH:
        diff = diff[:MAX_DIFF_LENGTH] + "\n... (truncated)"

    return diff


def save_diff(file_path, diff_text):
    output_path = Path(OUTPUT_DIR) / (file_path + ".diff")

    # 创建目录
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(diff_text)


def main(commit):
    Path(OUTPUT_DIR).mkdir(exist_ok=True)
    repo_root = get_repo_root()

    files = get_changed_files(commit, repo_root)
    print(f"\n✅ Found {len(files)} files after filtering\n")

    for f in files:
        try:
            diff = get_file_diff(commit, f, repo_root)

            if not diff.strip():
                print(f"Skipped (empty diff): {f}")
                continue

            save_diff(f, diff)
            print(f"Saved: {f}")

        except Exception as e:
            print(f"Error processing {f}: {e}")


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python codediff.py <commit>")
        exit(1)

    commit = sys.argv[1]
    main(commit)
