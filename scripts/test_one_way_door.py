# scripts/test_one_way_door.py — SSSF-48: the one-way-door classifier and the merge-danger block parser.
# Every fixture is made up: no customer data, names, numbers or secrets.
import os
import unittest

import one_way_door
from one_way_door import classify, decide, diff_paths, load_patterns, parse_danger_block

PATTERNS = load_patterns()


def file_diff(path, added=(), removed=(), old=None, new_file=False, deleted=False):
    """A minimal `git diff` section for one file. old= makes it a rename (old -> path)."""
    a = old or path
    out = [f"diff --git a/{a} b/{path}"]
    if old:
        out += ["similarity index 90%", f"rename from {old}", f"rename to {path}"]
    if new_file:
        out.append("new file mode 100644")
    if deleted:
        out.append("deleted file mode 100644")
    if added or removed:
        out.append("--- /dev/null" if new_file else f"--- a/{a}")
        out.append("+++ /dev/null" if deleted else f"+++ b/{path}")
        out.append(f"@@ -1,{len(removed)} +1,{len(added)} @@")
        out += [f"-{line}" for line in removed] + [f"+{line}" for line in added]
    return "\n".join(out) + "\n"


def ids(result):
    return sorted({r["id"] for r in result["reasons"]})


class SelfReportIgnored(unittest.TestCase):
    def test_forced_path_with_two_way_body_is_one_way(self):
        path = "supabase/migrations/024_new_table.sql"
        diff = file_diff(path, added=["CREATE TABLE example_rows (id int primary key);"], new_file=True)
        body = "Adds a table.\n\ndoor: two-way\nblast: local\n"
        d = decide(body, [path], diff, "BJGLLC/cd-dashboard", PATTERNS)
        self.assertEqual(d["door"], "one-way")
        self.assertTrue(d["forced"])
        self.assertEqual(d["declared"]["door"], "two-way")
        self.assertIn("sql-migration", ids(d))
        self.assertTrue(any("ignored" in n for n in d["notes"]), d["notes"])

    def test_body_can_opt_in_to_one_way(self):
        d = decide("door: one-way\nblast: customer\n", ["README.md"], file_diff("README.md", added=["x"]),
                   "BJGLLC/lead-agent", PATTERNS)
        self.assertEqual(d["door"], "one-way")
        self.assertFalse(d["forced"])
        self.assertEqual(d["reasons"], [])


class OrdinaryChanges(unittest.TestCase):
    def test_docs_change_is_two_way(self):
        diff = file_diff("docs/guide.md", added=["Run the tests before you push.", "DELETE FROM is explained here."])
        r = classify(["docs/guide.md"], diff, "BJGLLC/bjg-intranet", PATTERNS)
        self.assertEqual(r["door"], "two-way")
        self.assertEqual(r["reasons"], [])

    def test_test_change_is_two_way_even_when_it_mocks_a_send(self):
        path = "tests/test_router.py"
        diff = file_diff(path, added=['    assert posts[0][0].endswith("/conversations/messages")',
                                      '    payload = {"type": "SMS", "contactId": "ct-fake"}'])
        r = classify([path], diff, "BJGLLC/lead-agent", PATTERNS)
        self.assertEqual(r["door"], "two-way", r["reasons"])

    def test_ordinary_code_change_is_two_way(self):
        path = "core/metrics.ts"
        diff = file_diff(path, added=["export const total = (xs: number[]) => xs.reduce((a, b) => a + b, 0);"])
        self.assertEqual(classify([path], diff, "BJGLLC/cd-dashboard", PATTERNS)["door"], "two-way")


class Renames(unittest.TestCase):
    def test_rename_into_forced_path(self):
        old, new = "drafts/025_cleanup.sql.txt", "supabase/migrations/025_cleanup.sql"
        r = classify([new], file_diff(new, old=old), "BJGLLC/cd-dashboard", PATTERNS)
        self.assertEqual(r["door"], "one-way")
        self.assertIn(new, {x["path"] for x in r["reasons"]})

    def test_rename_out_of_forced_path_reads_the_old_name_from_the_diff(self):
        # gh's file list names only the new path; the classifier must still see where it came from.
        old, new = "supabase/migrations/023_old.sql", "archive/023_old.sql"
        r = classify([new], file_diff(new, old=old), "BJGLLC/cd-dashboard", PATTERNS)
        self.assertEqual(r["door"], "one-way")
        self.assertIn(old, {x["path"] for x in r["reasons"]})

    def test_diff_paths_lists_both_sides_of_a_rename_and_a_delete(self):
        diff = file_diff("b.py", old="a.py") + file_diff("gone.py", removed=["x = 1"], deleted=True)
        self.assertEqual(diff_paths(diff), ["a.py", "b.py", "gone.py"])

    def test_deleting_a_migration_is_one_way(self):
        path = "supabase/migrations/001_base_tables.sql"
        r = classify([path], file_diff(path, removed=["CREATE TABLE t (id int);"], deleted=True),
                     "BJGLLC/cd-dashboard", PATTERNS)
        self.assertEqual(r["door"], "one-way")


class ContentOnUnlistedPath(unittest.TestCase):
    def test_new_migration_statement_on_an_unlisted_path(self):
        path = "lib/store/setup.py"
        diff = file_diff(path, added=['    con.execute("ALTER TABLE example_rows ADD COLUMN note TEXT")'])
        r = classify([path], diff, "BJGLLC/lead-agent", PATTERNS)
        self.assertEqual(r["door"], "one-way")
        self.assertIn("schema-change-sql", ids(r))

    def test_delete_statement_on_an_unlisted_path(self):
        path = "app/cleanup.ts"
        diff = file_diff(path, added=['  db.exec("DELETE FROM example_rows");'])
        r = classify([path], diff, "BJGLLC/bjg-intranet", PATTERNS)
        self.assertEqual(r["door"], "one-way")
        self.assertIn("unscoped-delete-sql", ids(r))

    def test_scoped_delete_is_not_forced(self):
        # Clearing one row of local bookkeeping by key is routine in lead-agent; only an unscoped one forces.
        path = "runtime/alerts.py"
        diff = file_diff(path, added=['        con.execute("DELETE FROM example_alerts WHERE contact_id=?", (cid,))'])
        self.assertEqual(classify([path], diff, "BJGLLC/lead-agent", PATTERNS)["door"], "two-way")

    def test_removed_line_does_not_force(self):
        path = "app/cleanup.ts"
        diff = file_diff(path, removed=['  db.exec("DROP TABLE example_rows");'])
        self.assertEqual(classify([path], diff, "BJGLLC/bjg-intranet", PATTERNS)["door"], "two-way")

    def test_customer_send_endpoint_in_runtime_code(self):
        path = "runtime/new_flow.py"
        diff = file_diff(path, added=['    url = GHL_BASE + "/conversations/messages"'])
        r = classify([path], diff, "BJGLLC/lead-agent", PATTERNS)
        self.assertEqual(r["door"], "one-way")
        self.assertIn("ghl-send-endpoint", ids(r))
        self.assertIn("customer", r["blast"])

    def test_reading_one_message_is_not_a_send(self):
        path = "runtime/new_flow.py"
        diff = file_diff(path, added=['    detail = get(f"/conversations/messages/{message_id}")'])
        self.assertEqual(classify([path], diff, "BJGLLC/lead-agent", PATTERNS)["door"], "two-way")


class EmptyAndScope(unittest.TestCase):
    def test_empty_diff_is_two_way(self):
        r = classify([], "", "BJGLLC/lead-agent", PATTERNS)
        self.assertEqual(r, {"door": "two-way", "reasons": [], "blast": []})

    def test_repo_scoped_pattern_only_fires_in_its_repo(self):
        p = {"content_exempt_paths": [], "content_patterns": [], "path_patterns": [
            {"id": "only-here", "blast": "customer", "globs": ["n8n/workflows/**"], "repos": ["BJGLLC/deploy-a"],
             "why": "w", "source": {"repo": "BJGLLC/deploy-a", "example": "n8n/workflows/x.json"}}]}
        path = "n8n/workflows/abc.json"
        self.assertEqual(classify([path], "", "BJGLLC/deploy-a", p)["door"], "one-way")
        self.assertEqual(classify([path], "", "BJGLLC/other", p)["door"], "two-way")
        self.assertEqual(classify([path], "", "deploy-a", p)["door"], "one-way")  # bare name accepted

    def test_glob_double_star_matches_at_root_and_nested(self):
        self.assertTrue(one_way_door.glob_match("**/migrations/**", "migrations/001.sql"))
        self.assertTrue(one_way_door.glob_match("**/migrations/**", "supabase/migrations/001.sql"))
        self.assertFalse(one_way_door.glob_match("**/migrations/**", "docs/migrations.md"))
        self.assertFalse(one_way_door.glob_match("*.sql", "a/b.sql"))


class DangerBlock(unittest.TestCase):
    def test_missing_block(self):
        b = parse_danger_block("Just a summary.\n\nNo fields here.")
        self.assertIsNone(b["door"]); self.assertIsNone(b["blast"])
        self.assertEqual(b["errors"], ["missing merge-danger block (door: one-way | two-way, blast: local | repo | customer | money)"])

    def test_none_body_is_missing(self):
        self.assertIsNone(parse_danger_block(None)["door"])

    def test_malformed_values(self):
        b = parse_danger_block("door: maybe\nblast: galaxy\n")
        self.assertIsNone(b["door"]); self.assertIsNone(b["blast"])
        self.assertEqual(len(b["errors"]), 2, b["errors"])

    def test_half_a_block(self):
        b = parse_danger_block("door: two-way\n")
        self.assertEqual(b["door"], "two-way")
        self.assertEqual(b["errors"], ["blast: missing"])

    def test_both_fields_present(self):
        b = parse_danger_block("## Merge danger\n- door: One-Way\n- **blast:** customer\napproval: court 1121-q7\n")
        self.assertEqual((b["door"], b["blast"], b["approval"], b["errors"]), ("one-way", "customer", "1121-q7", []))

    def test_conflicting_repeats_are_an_error(self):
        b = parse_danger_block("door: two-way\nblast: repo\n...\ndoor: one-way\n")
        self.assertIsNone(b["door"])
        self.assertTrue(any("conflict" in e for e in b["errors"]), b["errors"])

    def test_fenced_example_is_not_the_block(self):
        b = parse_danger_block("How to fill it in:\n```\ndoor: two-way\nblast: repo\n```\n")
        self.assertIsNone(b["door"])


class ShippedPatternFile(unittest.TestCase):
    """The data file is the product: every entry is grounded, compiles, and fires on its own example."""

    def test_file_covers_sends_money_schema_deletes_and_secrets(self):
        kinds = {p["kind"] for p in PATTERNS["path_patterns"] + PATTERNS["content_patterns"]}
        self.assertTrue({"send", "money", "migration", "delete", "secret"} <= kinds, kinds)

    def test_every_pattern_names_its_source(self):
        for p in PATTERNS["path_patterns"] + PATTERNS["content_patterns"]:
            self.assertTrue(p["source"]["repo"].startswith("BJGLLC/"), p["id"])
            self.assertTrue(p["source"]["example"], p["id"])
            self.assertIn(p["blast"], one_way_door.BLASTS, p["id"])

    def test_every_path_pattern_fires_on_its_example(self):
        for p in PATTERNS["path_patterns"]:
            ex, repo = p["source"]["example"], p["source"]["repo"]
            r = classify([ex], "", repo, PATTERNS)
            self.assertIn(p["id"], ids(r), f"{p['id']} does not match its own example {ex}")

    def test_every_content_pattern_fires_on_its_fixture_and_only_on_added_lines(self):
        for p in PATTERNS["content_patterns"]:
            path, repo = p["source"]["example"], p["source"]["repo"]
            self.assertIn(p["id"], ids(classify([path], file_diff(path, added=[p["fixture"]]), repo, PATTERNS)), p["id"])
            self.assertNotIn(p["id"], ids(classify([path], file_diff(path, removed=[p["fixture"]]), repo, PATTERNS)), p["id"])


if __name__ == "__main__":
    unittest.main()
