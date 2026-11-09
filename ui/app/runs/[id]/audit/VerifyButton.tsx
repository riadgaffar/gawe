"use client";
import { useActionState } from "react";
import { verifyAudit, type VerifyState } from "@/app/actions";
import type { AuditVerificationView } from "@/lib/api-types";
import { SubmitButton } from "@/components/SubmitButton";
import { anchorNotice, shortHash } from "@/lib/format";

export function VerifyButton({ runId }: { runId: string }) {
  const [state, action] = useActionState<VerifyState, FormData>(verifyAudit, null);
  const result = state?.result;
  return (
    <form action={action} className="stack">
      <input type="hidden" name="run_id" value={runId} />
      <div className="row">
        <SubmitButton>Verify hash chain</SubmitButton>
        {state?.error && <span className="notice error">{state.error}</span>}
        {result?.ok && (
          <span className="notice ok" role="status">
            Chain intact: {result.checked} events, head {shortHash(result.head)}
          </span>
        )}
        {result && !result.ok && (
          <span className="notice error" role="alert">
            Chain BROKEN at event {result.failed_seq ?? "?"}: {result.reason ?? "unknown reason"}
          </span>
        )}
      </div>
      {result && <AnchorLine result={result} />}
    </form>
  );
}

function AnchorLine({ result }: { result: AuditVerificationView }) {
  const notice = anchorNotice(result.anchor);
  return (
    <p className={`notice ${notice.tone}`} role={notice.tone === "error" ? "alert" : "status"}>
      {notice.text}
    </p>
  );
}
