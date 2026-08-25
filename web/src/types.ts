export interface TextChange {
  file: string;
  before: string;
  after: string;
}

export interface RequestItem {
  id: number;
  user: string;
  author?: string;
  status: string;
  body: string;
  title?: string | null;
  summary?: string | null;
  user_visible: string[];
  notes?: string | null;
  risk?: string | null;
  question?: string | null;
  branch?: string | null;
  pr_number?: number | null;
  pr_url?: string | null;
  checks_status?: string | null;
  checks_detail?: string | null;
  files: string[];
  text_changes: TextChange[];
  images: string[];
  error?: string | null;
  created_at: string;
  updated_at: string;
  merged_at?: string | null;
  deployed_at?: string | null;
}

export interface EventItem {
  request_id?: number;
  ts: string;
  kind: string;
  text: string;
}

export interface Me {
  login: string;
  display_name: string;
  role: string;
  brand: { name: string; subtitle: string; accent: string };
  project: { site: string; repo: string };
}

export interface AdminOverview {
  error?: string;
  config_problems?: string[];
  repo?: { ok: boolean; base?: string; head?: string; last_commit?: string; error?: string };
  github?: { ok: boolean; repo?: string; can_push?: boolean; error?: string };
  deploy_mode?: string;
  sandbox?: { mode: string; network: boolean; model: string };
  access_links?: { login: string; display_name: string; role: string; link: string }[];
  runtime?: { repo_ready: boolean; repo_error: string; max_concurrent: number };
}

export type StreamPayload =
  | { type: "hello" }
  | { type: "request"; request: RequestItem }
  | ({ type: "event" } & EventItem);
