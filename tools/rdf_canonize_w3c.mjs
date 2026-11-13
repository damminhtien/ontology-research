import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const [packageRoot, casesPath] = process.argv.slice(2);
const require = createRequire(path.join(packageRoot, "package.json"));
const packageJson = require("rdf-canonize/package.json");
if (packageJson.version !== "5.0.0") {
  throw new Error(`Expected rdf-canonize 5.0.0, got ${packageJson.version}`);
}
const rdfCanonize = require("rdf-canonize");
const cases = JSON.parse(fs.readFileSync(casesPath, "utf8"));

for (const testCase of cases) {
  const input = fs.readFileSync(testCase.input, "utf8");
  const output = await rdfCanonize.canonize(rdfCanonize.NQuads.parse(input), {
    algorithm: "RDFC-1.0",
    messageDigestAlgorithm: testCase.hashAlgorithm,
    maxWorkFactor: 3,
  });
  const expected = fs.readFileSync(testCase.expected, "utf8");
  if (output.trimEnd() !== expected.trimEnd()) {
    throw new Error(`${testCase.name}: canonical N-Quads differ from the W3C vector`);
  }
}

const first = "_:left <urn:example:predicate> _:right .\n";
const renamed = "_:second <urn:example:predicate> _:first .\n";
const changed = "_:left <urn:example:other> _:right .\n";
const canonical = async (input) =>
  rdfCanonize.canonize(rdfCanonize.NQuads.parse(input), {
    algorithm: "RDFC-1.0",
    maxWorkFactor: 3,
  });
if ((await canonical(first)) !== (await canonical(renamed))) {
  throw new Error("blank-node renaming changed canonical output");
}
if ((await canonical(first)) === (await canonical(changed))) {
  throw new Error("different RDF datasets received the same canonical output");
}

console.log(`RDFC-1.0 W3C vectors passed (${cases.length}); blank-node invariance passed.`);
