param(
  [string]$Bucket = "litheca-mind-reader-runtime-staging"
)

$ErrorActionPreference = "Stop"
$apiRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$siteData = (Resolve-Path (Join-Path $apiRoot "..\bookhub\games\data\akinator")).Path
$wrangler = Join-Path $apiRoot "worker\mind-reader-staging\node_modules\.bin\wrangler.cmd"

$files = @(
  "admin_corrections.json", "author_overrides.json", "authors.json",
  "books.json", "cold_questions.json", "display_overrides.json",
  "excluded.json", "exclusive_overrides.json", "matrix.bin", "meta.json",
  "overrides.json", "question_candidates.json", "question_dependencies.json",
  "question_policy.json", "questions.json"
)

if (-not (Test-Path -LiteralPath $wrangler)) {
  throw "Wrangler is missing at $wrangler"
}

foreach ($name in $files) {
  $source = Join-Path $siteData $name
  if (-not (Test-Path -LiteralPath $source)) { throw "Missing admin artifact: $source" }
  & $wrangler r2 object put "$Bucket/admin/v1/$name" --file $source --remote
  if ($LASTEXITCODE -ne 0) { throw "Upload failed: $name" }
}

Write-Output "Uploaded $($files.Count) private admin artifacts to $Bucket/admin/v1/."
