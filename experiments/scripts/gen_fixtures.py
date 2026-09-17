#!/usr/bin/env python3
"""Generate all benchmark fixtures deterministically.

fixtures/
  repo/      2k-file git repo, 20 modules x 100 files (used by exp1/4/6/9/10)
  bugrepo/   small python package with a failing unittest (fix-bug tasks)
  webapp/    small node package with a failing test (node tasks)
  bigrepo/   50k-file git repo, 500 pkgs x 100 files (exp8, large-repo tasks)

Usage: python3 gen_fixtures.py [repo|bugrepo|webapp|bigrepo|all]
"""
import os, random, string, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.abspath(os.path.join(HERE, "..", "fixture"))


def git_commit(path):
    subprocess.run(["git", "-C", path, "add", "-A"], check=True)
    r = subprocess.run(["git", "-C", path, "-c", "user.email=t@t",
                        "-c", "user.name=t", "commit", "-qm", "init"])
    if r.returncode != 0:
        print(f"  (commit skipped in {path}: nothing new)")


def gen_repo():
    path = os.path.join(FIX, "repo")
    os.makedirs(path, exist_ok=True)
    random.seed(42)
    for d in range(20):
        os.makedirs(f"{path}/src/module{d:02d}", exist_ok=True)
        for f in range(100):
            with open(f"{path}/src/module{d:02d}/file{f:03d}.py", "w") as fh:
                fh.write("# " + "".join(random.choices(string.ascii_letters, k=200))
                         + "\n" * random.randint(5, 40))
                fh.write(f"import os\nimport sys\n\ndef foo_{f:03d}():\n    return {f}\n")
    subprocess.run(["git", "-C", path, "init", "-q"], check=True)
    git_commit(path)
    print("repo:", sum(len(fs) for _, _, fs in os.walk(path)), "files")


def gen_bugrepo():
    path = os.path.join(FIX, "bugrepo")
    os.makedirs(f"{path}/calc", exist_ok=True)
    os.makedirs(f"{path}/tests", exist_ok=True)
    open(f"{path}/calc/__init__.py", "w").close()
    with open(f"{path}/calc/ops.py", "w") as f:
        f.write('''def add(a, b):
    return a + b

def sub(a, b):
    return a - b

def mul(a, b):
    return a * b

def div(a, b):
    # BUG: integer division loses precision
    return a // b

def avg(nums):
    total = 0
    for n in nums:
        total = add(total, n)
    return div(total, len(nums))
''')
    with open(f"{path}/tests/test_ops.py", "w") as f:
        f.write('''import unittest
from calc.ops import add, sub, mul, div, avg

class TestOps(unittest.TestCase):
    def test_add(self): self.assertEqual(add(2, 3), 5)
    def test_sub(self): self.assertEqual(sub(5, 3), 2)
    def test_mul(self): self.assertEqual(mul(4, 3), 12)
    def test_div(self): self.assertEqual(div(7, 2), 3.5)
    def test_avg(self): assertAlmostEqual(avg([1, 2, 4]), 2.333333, places=4)

if __name__ == "__main__":
    unittest.main()
'''.replace("assertAlmostEqual", "self.assertAlmostEqual"))
    subprocess.run(["git", "-C", path, "init", "-q"], check=True)
    git_commit(path)
    print("bugrepo ready")


def gen_webapp():
    path = os.path.join(FIX, "webapp")
    os.makedirs(f"{path}/src", exist_ok=True)
    with open(f"{path}/package.json", "w") as f:
        f.write('{"name": "webapp", "version": "1.0.0", "scripts": {"test": "node test.js"}}\n')
    with open(f"{path}/src/utils.js", "w") as f:
        f.write('''function sum(list) {
  let total = 0;
  for (const x of list) total += x;
  return total;
}

function mean(list) {
  // BUG: integer truncation
  return Math.floor(sum(list) / list.length);
}

function uniq(list) {
  return [...new Set(list)];
}

module.exports = { sum, mean, uniq };
''')
    with open(f"{path}/test.js", "w") as f:
        f.write('''const assert = require("assert");
const { sum, mean, uniq } = require("./src/utils");

assert.strictEqual(sum([1, 2, 3]), 6);
assert.ok(Math.abs(mean([1, 2, 4]) - 2.333333) < 1e-4);
assert.deepStrictEqual(uniq([1, 1, 2, 3, 3]), [1, 2, 3]);
console.log("ALL TESTS PASSED");
''')
    print("webapp ready")


def gen_bigrepo(n_files=50000):
    path = os.path.join(FIX, "bigrepo")
    os.makedirs(path, exist_ok=True)
    random.seed(7)
    for d in range(500):
        os.makedirs(f"{path}/pkg{d:03d}", exist_ok=True)
        for f in range(n_files // 500):
            with open(f"{path}/pkg{d:03d}/mod{f:03d}.py", "w") as fh:
                fh.write("# " + "".join(random.choices(string.ascii_letters, k=150)) + "\n" * 20)
                fh.write(f"def fn_{d}_{f}():\n    return {f}\n")
    subprocess.run(["git", "-C", path, "init", "-q"], check=True)
    git_commit(path)
    print("bigrepo:", n_files, "files")


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    os.makedirs(FIX, exist_ok=True)
    {"repo": gen_repo, "bugrepo": gen_bugrepo, "webapp": gen_webapp,
     "bigrepo": gen_bigrepo, "all": lambda: [gen_repo(), gen_bugrepo(),
                                             gen_webapp(), gen_bigrepo()]}[which]()
