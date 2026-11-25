import subprocess
import tempfile
import unittest
from pathlib import Path

from .backend import generate


class BackendTest(unittest.TestCase):
    def compile(self, program):
        directory = tempfile.TemporaryDirectory()
        c = Path(directory.name) / "program.c"
        exe = Path(directory.name) / "program"
        c.write_text(generate(program))
        subprocess.run(["clang", "-std=c11", "-O2", str(c), "-o", str(exe)], check=True)
        return directory, exe

    def test_dynamic_rows_and_u32(self):
        d, exe = self.compile(["emit", ["u32", ["mul", ["at", 0], ["at", 1]]]])
        try:
            result = subprocess.run([exe], input="4294967295 3\n", text=True,
                                     capture_output=True, check=True)
            self.assertEqual(result.stdout, "4294967293\n")
        finally:
            d.cleanup()

    def test_malformed_and_overflow_input_rejected(self):
        d, exe = self.compile(["emit", ["at", 0]])
        try:
            for row in ("1-2\n", "999999999999999999999999\n"):
                result = subprocess.run([exe], input=row, text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
        finally:
            d.cleanup()

    def test_slot_limit_sizes_the_default_array(self):
        last = ["seq", ["set", 2047, ["at", 0]], ["emit", ["var", 2047]]]
        with self.assertRaises(ValueError):
            generate(last)
        self.assertIn("int64_t slots[1024]", generate(["emit", ["at", 0]]))
        self.assertIn("int64_t slots[2048]", generate(last, slots=2048))
        with self.assertRaises(ValueError):
            generate(["set", 2048, 1], slots=2048)
        directory = tempfile.TemporaryDirectory()
        try:
            c, exe = Path(directory.name) / "program.c", Path(directory.name) / "program"
            c.write_text(generate(last, slots=2048))
            subprocess.run(["clang", "-std=c11", "-O2", str(c), "-o", str(exe)], check=True)
            result = subprocess.run([exe], input="7\n", text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout, "7\n")
        finally:
            directory.cleanup()


if __name__ == "__main__":
    unittest.main()
