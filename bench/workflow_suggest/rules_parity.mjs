// Do the rules copied in bench.py give the same chips as the editor's code?
//
//   python bench/workflow_suggest/bench.py --dump-cases /tmp/cases.json
//   node bench/workflow_suggest/rules_parity.mjs <apowerb-ui>/src/lib /tmp/cases.json
//
// Runs the real suggestNextNodes and mergeAiSuggestions on every case and
// prints what they return; bench.py --check-parity compares it with its copy.
// Only the two route constants are taken from workflowGraph.js: the rest of
// that module (canvas geometry, node factory) plays no part in the ranking.
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const [libDir, casesPath] = process.argv.slice(2);
if (!libDir || !casesPath) {
  console.error("usage: node rules_parity.mjs <apowerb-ui>/src/lib <cases.json>");
  process.exit(2);
}

const constants = readFileSync(join(libDir, "workflowGraph.js"), "utf8").match(
  /^export const (CONDITION_ROUTES|TRY_ROUTES) = .*;$/gm,
);
if (constants?.length !== 2) throw new Error("CONDITION_ROUTES / TRY_ROUTES not found in workflowGraph.js");

const dir = mkdtempSync(join(tmpdir(), "rules-parity-"));
try {
  writeFileSync(
    join(dir, "workflowGraph.mjs"),
    `${constants.join("\n")}\nexport const NODE_BOX = {};\nexport function createNode() {}\nexport function findFreePosition() {}\nexport function graphToFlow() {}\n`,
  );
  const source = readFileSync(join(libDir, "nextNodeSuggestions.js"), "utf8");
  writeFileSync(join(dir, "lib.mjs"), source.replace('from "@/lib/workflowGraph"', 'from "./workflowGraph.mjs"'));
  const { suggestNextNodes, mergeAiSuggestions } = await import(pathToFileURL(join(dir, "lib.mjs")).href);

  const out = {};
  for (const c of JSON.parse(readFileSync(casesPath, "utf8"))) {
    // Canvas shape: config under data, route on the edge's data.
    const nodes = c.graph.nodes.map((n) => ({ id: n.id, type: n.type, data: { config: n.config || {} } }));
    const edges = c.graph.edges.map((e) => ({ source: e.source, target: e.target, data: { route: e.route } }));
    const rules = suggestNextNodes(nodes.find((n) => n.id === c.node_id), nodes, edges);
    // A fixed model answer, to exercise the merge on every case.
    const ai = [{ type: c.expected.type }, { type: "output" }];
    out[c.id] = {
      rules: rules.map((s) => ({ type: s.type, route: s.route ?? null })),
      merged: mergeAiSuggestions(rules, ai).map((s) => s.type),
    };
  }
  process.stdout.write(JSON.stringify(out));
} finally {
  rmSync(dir, { recursive: true, force: true });
}
