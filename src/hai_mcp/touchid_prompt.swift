// hai-touchid — spawned by the hai-mcp server, never by the MCP client.
//
// Reads one JSON object from stdin:
//   {"reason": "...", "fingerprint": "sha256:...", "timeout_seconds": 60}
// Shows a macOS LocalAuthentication prompt (Touch ID, Face ID, or the
// device password) whose reason text is the string the server composed.
// Writes one JSON object to stdout and exits:
//   0 approved, 1 owner cancelled or failed, 2 timed out, 3 unavailable.
// Does not take a code and does not print one. Recent Touch ID unlocks are
// not reused (touchIDAuthenticationAllowableReuseDuration = 0).
//
// The main thread must keep running a dispatch loop: the system dialog will
// not complete if the process blocks the main thread on a semaphore.

import Foundation
import LocalAuthentication

struct PromptRequest: Decodable {
    let reason: String
    let fingerprint: String
    let timeout_seconds: Double
}

struct PromptResponse: Encodable {
    let ok: Bool
    let fingerprint: String
    let reason: String
    let error: String?
    let method: String?
}

final class Once: @unchecked Sendable {
    private let lock = NSLock()
    private var finished = false

    func claim() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        if finished {
            return false
        }
        finished = true
        return true
    }
}

func emit(_ response: PromptResponse, code: Int32) -> Never {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.sortedKeys]
    if let data = try? encoder.encode(response) {
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data("\n".utf8))
    }
    fflush(stdout)
    exit(code)
}

func fail(_ fingerprint: String, _ reason: String, _ error: String, _ code: Int32) -> Never {
    FileHandle.standardError.write(Data("hai-touchid: \(error)\n".utf8))
    emit(
        PromptResponse(ok: false, fingerprint: fingerprint, reason: reason, error: error, method: nil),
        code: code
    )
}

func classify(_ error: Error?) -> (Int32, String) {
    guard let la = error as? LAError else {
        return (1, "auth_failed")
    }
    switch la.code {
    case .userCancel, .appCancel, .systemCancel, .userFallback:
        return (1, "user_cancel")
    case .authenticationFailed:
        return (1, "auth_failed")
    case .biometryNotAvailable, .biometryNotEnrolled, .biometryLockout, .passcodeNotSet:
        return (3, "unavailable")
    default:
        return (1, "auth_failed")
    }
}

func biometryName(_ context: LAContext) -> String {
    switch context.biometryType {
    case .touchID:
        return "touchid"
    case .faceID:
        return "faceid"
    default:
        return "device_owner"
    }
}

let raw = FileHandle.standardInput.readDataToEndOfFile()
guard let request = try? JSONDecoder().decode(PromptRequest.self, from: raw) else {
    fail("", "", "unavailable", 3)
}

// Echo `request.reason` unchanged so the server can match it to the challenge.
// Reject a blank reason before the dialog; an empty localizedReason fails the prompt.
let shown = request.reason.trimmingCharacters(in: .whitespacesAndNewlines)
if shown.isEmpty || request.fingerprint.isEmpty || request.timeout_seconds <= 0 {
    fail(request.fingerprint, request.reason, "unavailable", 3)
}

let context = LAContext()
context.touchIDAuthenticationAllowableReuseDuration = 0
var policyError: NSError?
// deviceOwnerAuthentication: Touch ID / Face ID, or the device password.
let policy: LAPolicy = .deviceOwnerAuthentication
guard context.canEvaluatePolicy(policy, error: &policyError) else {
    fail(request.fingerprint, request.reason, "unavailable", 3)
}

let wait = min(max(request.timeout_seconds, 1), 180)
let once = Once()

context.evaluatePolicy(policy, localizedReason: request.reason) { success, evalError in
    guard once.claim() else { return }
    if success {
        emit(
            PromptResponse(
                ok: true,
                fingerprint: request.fingerprint,
                reason: request.reason,
                error: nil,
                method: biometryName(context)
            ),
            code: 0
        )
    } else {
        let (code, name) = classify(evalError)
        fail(request.fingerprint, request.reason, name, code)
    }
}

DispatchQueue.main.asyncAfter(deadline: .now() + wait) {
    guard once.claim() else { return }
    context.invalidate()
    fail(request.fingerprint, request.reason, "timeout", 2)
}

dispatchMain()
