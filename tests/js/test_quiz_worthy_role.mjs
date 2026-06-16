// Node-based unit test for review.js:quizWorthyRole().
//
// review.js is a browser script (it calls document.* at top level), so it can't
// be require()'d directly. We extract just the quizWorthyRole function from the
// real source and evaluate it in isolation — this tests the shipped code, not a
// copy, and runs under the built-in `node --test` runner with no extra deps:
//
//   node --test tests/js/
//
// The role-resolution rule mirrors the Python side, which is independently
// covered by tests/test_bugs.py::TestGetStateReuploadAndColorConfig.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(
  join(here, "..", "..", "src", "zsg", "static", "review.js"), "utf-8");

// Pull the function definition straight out of the source file.
const match = source.match(/function quizWorthyRole\([\s\S]*?\n\}/);
assert.ok(match, "could not locate quizWorthyRole in review.js");
// eslint-disable-next-line no-eval
const quizWorthyRole = eval(`(${match[0]})`);

const DEFAULT = {
  red: { label: "Quiz-worthy facts" },
  yellow: { label: "Key concepts" },
};

test("resolves the default red mapping", () => {
  assert.deepEqual(quizWorthyRole(DEFAULT),
    { color: "red", label: "Quiz-worthy facts" });
});

test("honors a remapped Quiz-worthy color", () => {
  // Instructor reassigned the Quiz-worthy role to purple in color_config.
  const remapped = {
    red: { label: "Key dates" },
    purple: { label: "Quiz-worthy items" },
  };
  assert.deepEqual(quizWorthyRole(remapped),
    { color: "purple", label: "Quiz-worthy items" });
});

test("falls back to red when no color carries the role", () => {
  const noRole = { blue: { label: "Themes" } };
  assert.deepEqual(quizWorthyRole(noRole),
    { color: "red", label: "Quiz-worthy facts" });
});

test("falls back to red on an empty config", () => {
  assert.deepEqual(quizWorthyRole({}),
    { color: "red", label: "Quiz-worthy facts" });
});
