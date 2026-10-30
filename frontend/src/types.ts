export interface Session {
  authenticated: boolean;
  username: string;
  is_admin: boolean;
}

export interface Health {
  status: string;
  build_version: string;
  mock_external_services: boolean;
  llm_configured: boolean;
  external_api_configured: boolean;
  agents: number;
  model_connections: number;
  ask_concurrency_limit: number;
  ask_active: number;
  ask_rejected: number;
  ldap_enabled: boolean;
}

export interface AgentSummary {
  agent_id: string;
  name: string;
  description: string;
  is_default: boolean;
  allowed_tools: string[];
}

export interface MetadataValue {
  value: unknown;
  confidence?: number | null;
  evidence?: string;
  location?: string;
  extraction_method?: string;
}

export interface Source {
  source_id: string;
  filename: string;
  file_hash: string;
  document_type: string;
  mime_type: string;
  standard_no?: string | null;
  standard_title?: string | null;
  standard_year?: number | null;
  parse_status: string;
  dify_status: string;
  pdf_platform_status: string;
  metadata_status: string;
  rewrite_status: string;
  custom_metadata: Record<string, MetadataValue>;
  created_at: string;
  updated_at: string;
  error_message?: string | null;
  metadata_error?: string | null;
  pdf_url?: string | null;
}

export interface SourcePage {
  items: Source[];
  page: number;
  page_size: number;
  total: number;
  total_pages: number;
}

export interface SourceStats {
  total: number;
  parsed: number;
  indexed: number;
  failed: number;
  processing: number;
}

export interface MarkdownPreview {
  source_id: string;
  filename: string;
  version: string;
  markdown: string;
  available_versions: string[];
  stats: Record<string, unknown>;
}

export interface JobItem {
  item_id: string;
  position: number;
  filename: string;
  source_id?: string | null;
  status: string;
  message: string;
  attempts?: number;
}

export interface IngestJob {
  job_id: string;
  status: string;
  total: number;
  uploaded: number;
  completed: number;
  succeeded: number;
  failed: number;
  preview_ready: number;
  items: JobItem[];
  created_at: string;
  updated_at?: string | null;
}

export interface DeleteJob {
  job_id: string;
  agent_id: string;
  knowledge_base_id: string;
  knowledge_base_name: string;
  status: string;
  total: number;
  completed: number;
  succeeded: number;
  failed: number;
  items: JobItem[];
  created_at: string;
  updated_at?: string | null;
}

export interface TaskSummary {
  task_id: string;
  task_type: 'document_ingest' | 'document_delete';
  agent_id: string;
  title: string;
  status: 'PENDING' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED';
  raw_status: string;
  total: number;
  completed: number;
  succeeded: number;
  failed: number;
  review_required: number;
  created_at: string;
  updated_at: string;
  can_pause: boolean;
  can_resume: boolean;
  can_retry: boolean;
}

export interface TaskPage {
  items: TaskSummary[];
  page: number;
  page_size: number;
  total: number;
  total_pages: number;
}

export interface KnowledgeBase {
  knowledge_base_id: string;
  owner_username: string;
  name: string;
  description: string;
  base_url: string;
  dataset_id: string;
  search_method: string;
  chunking_mode: string;
  chunk_separator: string;
  chunk_max_tokens: number;
  pdf_parser: string;
  enabled: boolean;
  api_key_configured: boolean;
  metadata_fields: Array<{key: string; label: string; value_type: string; description: string}>;
  [key: string]: unknown;
}

export interface Evidence {
  evidence_id: string;
  source_id: string;
  filename: string;
  page?: number | null;
  location?: string | null;
  content: string;
  retrieval_type: string;
  score?: number | null;
  pdf_url?: string | null;
}

export interface AskResponse {
  answer: string;
  agent_id: string;
  agent_name: string;
  tool_calls: number;
  evidences: Evidence[];
  trace?: Array<Record<string, unknown>>;
}
