// Smoke test: load the DSH command plugin with a fake command registry and
// execute the read-only commands against a running bot-team server.
//
//   BOT_TEAM_API=http://127.0.0.1:8799 node integrations/dsh-command-bot-team/smoke.mjs

import assert from "node:assert/strict";
import { apply, inject, name } from "./index.js";

assert.equal(name, "command-bot-team");
assert.deepEqual(inject, ["commands"]);

const registered = [];
apply({ commands: { register: (definition) => registered.push(definition) } });

assert.deepEqual(
  registered.map((item) => item.name),
  ["today", "queue", "park", "resume", "ticket", "decide", "defer", "focus", "bt-metrics"],
);

for (const command of ["today", "queue", "bt-metrics"]) {
  const definition = registered.find((item) => item.name === command);
  const result = await definition.handler({ rawInput: "", agent: null });
  assert.equal(result.kind, "success", `${command} failed: ${result.text}`);
  console.log(`--- /${command}\n${result.text}\n`);
}

console.log("OK: command-bot-team smoke passed");
