import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from trading_agent.cli import main
from trading_agent.data import synthetic_snapshot, write_csv


class CliExtensionTests(unittest.TestCase):
    def test_recorded_propose_then_research_runs_without_credentials(self):
        with TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            root = Path(directory)
            prices, proposal, database = root / "prices.csv", root / "proposal.json", root / "research.sqlite3"
            write_csv(synthetic_snapshot(160), prices)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = main(["propose", "--csv", str(prices), "--db", str(database),
                             "--recorded-response", "fixtures/llm-response.json", "--output", str(proposal)])
                self.assertEqual(code, 0)
                self.assertEqual(main(["research", "--csv", str(prices), "--db", str(database),
                                      "--proposal-file", str(proposal), "--output", str(root / "report")]), 0)
            artifact = json.loads(proposal.read_text())
            self.assertFalse(artifact["provenance"]["network_verified"])
            report = json.loads((root / "report/research.json").read_text())
            self.assertIn("proposal", report["configuration"])
            self.assertFalse(report["live_promotion"])

    def test_prepare_requires_no_credentials_and_does_not_overwrite_identity(self):
        with TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=True):
            output = Path(directory) / "intent.json"
            command = ["kis-paper", "prepare", "--symbol", "123456", "--side", "BUY", "--quantity", "1", "--price", "1000", "--output", str(output)]
            with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
                self.assertEqual(main(command), 0)
                original = output.read_text()
                self.assertFalse(json.loads(stdout.getvalue())["sent"])
                self.assertEqual(main(command), 2)
            self.assertEqual(output.read_text(), original)


if __name__ == "__main__":
    unittest.main()
