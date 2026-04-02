/**
 * Smoke test for sshTestGate() using astropy__astropy-12907.
 * Run: node scripts/test-ssh-gate.mjs
 */

import { sshTestGate } from "../dist/src/admission/ssh-test-gate.js"

const INSTANCE_ID = "astropy__astropy-12907"
const REPO = "astropy/astropy"
const FAIL_TO_PASS = [
  "astropy/modeling/tests/test_separable.py::test_cmp_separability",
  "astropy/modeling/tests/test_separable.py::test_cdot_cmp_separability",
  "astropy/modeling/tests/test_separable.py::test_amplitude_and_rotation",
]

const PATCH = `--- a/astropy/modeling/separable.py
+++ b/astropy/modeling/separable.py
@@ -225,7 +225,7 @@
         cright = _coord_matrix(right, 'right', noutp)
     else:
         cright = np.zeros((noutp, right.shape[1]))
-        cright[-right.shape[0]:, -right.shape[1]:] = 1
+        cright[-right.shape[0]:, -right.shape[1]:] = right

     return np.hstack([cleft, cright])
`

console.log("=== SSH+Docker Test Gate Smoke Test ===")
console.log(`instance: ${INSTANCE_ID}`)
console.log(`fail_to_pass: ${FAIL_TO_PASS.length} tests`)
console.log("(first run may take ~5-10 min to pull image)")
console.log("")

const SSH_HOST = process.env.SSH_EVAL_HOST ?? ""
if (!SSH_HOST) {
  console.error("ERROR: SSH_EVAL_HOST not set (eval now runs on ECS, not via SSH)")
  process.exit(1)
}

const result = sshTestGate(INSTANCE_ID, REPO, "1776", PATCH, FAIL_TO_PASS, {
  sshHost: SSH_HOST,
  timeoutMs: 900_000,
})

console.log("\n=== Result ===")
console.log("status:", result.status)
console.log("code:", result.code)
console.log("message:", result.message)
console.log("details:", JSON.stringify(result.details, null, 2))
