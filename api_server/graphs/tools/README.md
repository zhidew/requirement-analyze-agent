# Tool Extensions

## New tools

- `clone_repository`
  - Clone or update a configured Git repository into `projects/{project_id}/cloned_repos/{repo_id}`.
  - Inputs: `root_dir`, `repo_id`, optional `project_id`, `repo_url`, `branch`, `depth`.

- `query_database`
  - Query configured database metadata or execute read-only SQL.
  - Inputs: `root_dir`, `db_id`, `query_type`, optional `schema`, `table_name`, `sql`, `limit`.
  - Supported `query_type`: `list_tables`, `describe_table`, `list_indexes`, `list_constraints`, `execute_query`.
  - Supported database types in project config: `sqlite`, `mysql`, `postgresql`, `opengauss`, `dws`, `oracle`.

- `query_knowledge_base`
  - Search configured local knowledge bases for terminology, feature trees, and design documents.
  - Inputs: `root_dir`, optional `kb_id`, `query_type`, optional `keyword`, `feature_id`.
  - Supported `query_type`: `search_terms`, `get_feature_tree`, `search_design_docs`, `get_related_designs`.

## Multi-root file tools

`list_files`, `grep_search`, and `read_file_chunk` now support optional `repos_dir` for searching cloned repositories alongside the primary `root_dir`.
