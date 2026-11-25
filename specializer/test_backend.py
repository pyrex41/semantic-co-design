import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from .backend import generate, lower_c_body


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

    def test_overlapping_compilations_keep_their_own_limits(self):
        # The wide compilation enters validation, then the narrow one enters and stays there
        # until the wide one has finished: neither may see the other's limit.
        wide_in, narrow_in, wide_done = threading.Event(), threading.Event(), threading.Event()
        results = {}

        class Held(list):
            """A program that runs `hook` the first time validation looks inside it."""
            def __init__(self, program, hook):
                super().__init__(program)
                self.hook = hook

            def __getitem__(self, index):
                hook, self.hook = self.hook, None
                if hook:
                    hook()
                return list.__getitem__(self, index)

        def wide():
            program = Held(["set", 2047, ["at", 0]], lambda: (wide_in.set(), narrow_in.wait(10)))
            try:
                lower_c_body(program, slots=2048)
                results["wide"] = "accepted"
            except ValueError:
                results["wide"] = "rejected"
            wide_done.set()

        def narrow():
            wide_in.wait(10)
            program = Held(["set", 2, ["at", 0]], lambda: (narrow_in.set(), wide_done.wait(10)))
            try:
                lower_c_body(program, slots=2)
                results["narrow"] = "accepted"
            except ValueError:
                results["narrow"] = "rejected"

        threads = [threading.Thread(target=wide), threading.Thread(target=narrow)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(results, {"wide": "accepted", "narrow": "rejected"})
        # And the default is what it was.
        lower_c_body(["set", 1023, 1])
        with self.assertRaises(ValueError):
            lower_c_body(["set", 1024, 1])


if __name__ == "__main__":
    unittest.main()
