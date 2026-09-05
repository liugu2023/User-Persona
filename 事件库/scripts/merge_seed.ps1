# 合并 seed 分片 → contents.seed.json / ads.seed.json（purity 统一计算）
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$seedDir = Join-Path $root 'seed'

$taxonomyPath = Join-Path $root 'taxonomy.json'
if (-not (Test-Path -LiteralPath $taxonomyPath)) { throw "taxonomy.json 不存在：$taxonomyPath" }
$taxonomy = Get-Content -Raw -Encoding UTF8 $taxonomyPath | ConvertFrom-Json
$order = @($taxonomy.domain_order)
$names = @{}
foreach ($p in $taxonomy.domains.PSObject.Properties) { $names[$p.Name] = [string]$p.Value.cn }
if ($order.Count -eq 0 -or $names.Count -ne $order.Count) { throw 'taxonomy.json 的域顺序与域定义不完整' }

$contents = @(); $probes = @(); $ads = @()
foreach ($d in $order) {
  $fp = Join-Path $seedDir "$d.json"
  $j = Get-Content -Raw -Encoding UTF8 $fp | ConvertFrom-Json
  foreach ($it in @($j.interest)) {
    $reviewStatus = if ([string]::IsNullOrWhiteSpace([string]$it.review_status)) { 'pending' } else { [string]$it.review_status }
    $reviewedBy = if ($null -eq $it.reviewed_by) { '' } else { [string]$it.reviewed_by }
    $reviewedAt = if ($null -eq $it.reviewed_at) { '' } else { [string]$it.reviewed_at }
    $subN = @($it.sub_tags.PSObject.Properties).Count
    $trN  = @($it.traits.PSObject.Properties).Count
    $purity = if (($subN + $trN) -gt 0) { 1.0 / ($subN + $trN) } else { 1.0 }
    $obj = [ordered]@{
      content_id = $it.content_id; title = $it.title; summary = $it.summary; body = $it.body
      body_len = $it.body_len; domain = $it.domain; domain_cn = $names[$it.domain]
      sub_tags = $it.sub_tags; traits = $it.traits
      cover_type = 'css_gradient'; cover_theme = $it.cover_theme
      purity = [math]::Round($purity, 3); is_probe = $false
      review_status = $reviewStatus; reviewed_by = $reviewedBy; reviewed_at = $reviewedAt
    }
    $contents += [pscustomobject]$obj
  }
  foreach ($p in @($j.probes)) {
    $reviewStatus = if ([string]::IsNullOrWhiteSpace([string]$p.review_status)) { 'pending' } else { [string]$p.review_status }
    $reviewedBy = if ($null -eq $p.reviewed_by) { '' } else { [string]$p.reviewed_by }
    $reviewedAt = if ($null -eq $p.reviewed_at) { '' } else { [string]$p.reviewed_at }
    $obj = [ordered]@{
      content_id = $p.content_id; title = $p.title; summary = $p.summary; body = $p.body
      body_len = $p.body_len; domain = $p.domain; domain_cn = $names[$p.domain]
      sub_tags = $p.sub_tags; traits = @{}
      cover_type = 'css_gradient'; cover_theme = $p.cover_theme
      purity = 1.0; is_probe = $true
      review_status = $reviewStatus; reviewed_by = $reviewedBy; reviewed_at = $reviewedAt
    }
    $probes += [pscustomobject]$obj
  }
  foreach ($a in @($j.ads)) {
    $reviewStatus = if ([string]::IsNullOrWhiteSpace([string]$a.review_status)) { 'pending' } else { [string]$a.review_status }
    $reviewedBy = if ($null -eq $a.reviewed_by) { '' } else { [string]$a.reviewed_by }
    $reviewedAt = if ($null -eq $a.reviewed_at) { '' } else { [string]$a.reviewed_at }
    $ads += [pscustomobject]@{
      ad_id = $a.ad_id; title = $a.title; body = $a.body
      targeting = $a.targeting; reason_template = $a.reason_template; is_simulated = $true
      review_status = $reviewStatus; reviewed_by = $reviewedBy; reviewed_at = $reviewedAt
    }
  }
}

$ts = Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'
$out1 = [pscustomobject]@{
  version = 1; kind = 'contents_seed'; generated_at = $ts
  counts = [pscustomobject]@{ interest = $contents.Count; probes = $probes.Count; total = ($contents.Count + $probes.Count) }
  contents = @($contents) + @($probes)
}
$out2 = [pscustomobject]@{
  version = 1; kind = 'ads_seed'; generated_at = $ts; counts = [pscustomobject]@{ ads = $ads.Count }
  ads = @($ads)
}

$json1 = $out1 | ConvertTo-Json -Depth 12
$json2 = $out2 | ConvertTo-Json -Depth 12
$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $root 'contents.seed.json'), $json1, $utf8)
[System.IO.File]::WriteAllText((Join-Path $root 'ads.seed.json'), $json2, $utf8)
Write-Host "contents.seed.json: 兴趣 $($contents.Count) 条 + 探针 $($probes.Count) 条 = $($contents.Count + $probes.Count) 条"
Write-Host "ads.seed.json: 广告 $($ads.Count) 条"

# 同步生成人工审核可读版 md（保证 md 与种子永不脱节，改进建议 A.1）
& (Join-Path $PSScriptRoot 'gen_audit_md.ps1')
