// Typed client for the GAWE API. Used only on the server (pages, server actions, route
// handlers): the token comes from the httpOnly cookie and never reaches browser JavaScript.
import type {
  ApprovalView,
  AuditEventView,
  AuditVerificationView,
  DecisionInput,
  MeView,
  RunView,
  StartRunInput,
} from "./api-types";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

export type FetchLike = (url: string, init?: RequestInit) => Promise<Response>;

export type Client = ReturnType<typeof createClient>;

export function createClient(options: { baseUrl: string; token: string; fetchImpl?: FetchLike }) {
  const base = options.baseUrl.replace(/\/+$/, "");
  const doFetch: FetchLike = options.fetchImpl ?? ((url, init) => fetch(url, init));

  async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
    let response: Response;
    try {
      response = await doFetch(base + path, {
        method,
        cache: "no-store",
        headers: {
          Authorization: `Bearer ${options.token}`,
          ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch {
      throw new ApiError(0, "unreachable", "Cannot reach the GAWE API. Is `make api` running?");
    }
    if (!response.ok) throw await toError(response);
    return (await response.json()) as T;
  }

  const enc = encodeURIComponent;
  return {
    me: () => call<MeView>("GET", "/v1/me"),
    listRuns: (limit = 50) => call<RunView[]>("GET", `/v1/runs?limit=${limit}`),
    getRun: (id: string) => call<RunView>("GET", `/v1/runs/${enc(id)}`),
    startRun: (input: StartRunInput) => call<RunView>("POST", "/v1/runs", input),
    cancelRun: (id: string) => call<RunView>("POST", `/v1/runs/${enc(id)}/cancel`, {}),
    resumeRun: (id: string) => call<RunView>("POST", `/v1/runs/${enc(id)}/resume`, {}),
    audit: (id: string) => call<AuditEventView[]>("GET", `/v1/runs/${enc(id)}/audit`),
    verifyAudit: (id: string) =>
      call<AuditVerificationView>("GET", `/v1/runs/${enc(id)}/audit/verify`),
    inbox: () => call<ApprovalView[]>("GET", "/v1/approvals"),
    decide: (approvalId: string, input: DecisionInput) =>
      call<ApprovalView>("POST", `/v1/approvals/${enc(approvalId)}/decision`, input),
  };
}

async function toError(response: Response): Promise<ApiError> {
  let code = "error";
  let message = `The API answered ${response.status}.`;
  try {
    const data = (await response.json()) as { error?: { code?: string; message?: string } };
    if (data.error?.code) code = data.error.code;
    if (data.error?.message) message = data.error.message;
  } catch {
    // not JSON: keep the generic message
  }
  return new ApiError(response.status, code, message);
}
