# 由 seed/*.json 反向生成 8 份人工审核可读版 md（改进建议 A.1：单一事实来源是种子）
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$seedDir = Join-Path $root 'seed'

$taxonomyPath = Join-Path $root 'taxonomy.json'
if (-not (Test-Path -LiteralPath $taxonomyPath)) { throw "taxonomy.json 不存在：$taxonomyPath" }
$taxonomy = Get-Content -Raw -Encoding UTF8 $taxonomyPath | ConvertFrom-Json
$order = @($taxonomy.domain_order)
$names = @{}
foreach ($p in $taxonomy.domains.PSObject.Properties) { $names[$p.Name] = [string]$p.Value.cn }
$files = @{ tech_digital='01_tech_digital.md'; game_acg='02_game_acg.md'; life_shopping='03_life_shopping.md';
  study_exam='04_study_exam.md'; culture_art='05_culture_art.md'; sport_outdoor='06_sport_outdoor.md';
  social_hot='07_social_hot.md'; av_ent='08_av_ent.md' }
$traitShort = @{ price_sensitive='价敏'; premium_oriented='价优'; deep_reader='深读'; quick_skimmer='速览';
  early_adopter='尝鲜'; conservative='保守'; professional='专业'; casual='大众';
  decisive='果断'; comparer='对比' }
$inv = [Globalization.CultureInfo]::InvariantCulture
function Fmt($v) { return ([double]$v).ToString('0.0', $inv) }
function PairStr($obj, $shortMap) {
  $parts = foreach ($p in $obj.PSObject.Properties) {
    $label = if ($shortMap -and $shortMap[$p.Name]) { $shortMap[$p.Name] } else { $p.Name }
    '{0} {1}' -f $label, (Fmt $p.Value)
  }
  return ($parts -join ' / ')
}
$utf8 = New-Object System.Text.UTF8Encoding($false)

foreach ($d in $order) {
  $j = Get-Content -Raw -Encoding UTF8 (Join-Path $seedDir "$d.json") | ConvertFrom-Json
  $nI = @($j.interest).Count; $nP = @($j.probes).Count; $nA = @($j.ads).Count
  $sb = New-Object System.Text.StringBuilder
  [void]$sb.AppendLine("# $($names[$d]) · 内容成品（$nI 兴趣 + $nP 探针 + $nA 广告）")
  foreach ($it in @($j.interest) + @($j.probes)) {
    [void]$sb.AppendLine()
    [void]$sb.AppendLine("## $($it.content_id) $($it.title)")
    $tagStr = PairStr $it.sub_tags $null
    $trStr = if ($it.traits -and @($it.traits.PSObject.Properties).Count -gt 0) { PairStr $it.traits $traitShort } else { '无' }
    [void]$sb.AppendLine("- 域 $($names[$d])｜长度 $($it.body_len)｜标签 $tagStr｜特质 $trStr")
    $reviewStatus = if ([string]::IsNullOrWhiteSpace([string]$it.review_status)) { 'pending' } else { [string]$it.review_status }
    $reviewedBy = if ($null -eq $it.reviewed_by) { '' } else { [string]$it.reviewed_by }
    $reviewedAt = if ($null -eq $it.reviewed_at) { '' } else { [string]$it.reviewed_at }
    [void]$sb.AppendLine("- 审核：$reviewStatus" + $(if ($reviewedBy) { "｜审核人 $reviewedBy" } else { '' }) + $(if ($reviewedAt) { "｜时间 $reviewedAt" } else { '' }))
    [void]$sb.AppendLine("- 摘要：$($it.summary)")
    $paras = $it.body -split "`n`n"
    [void]$sb.AppendLine("- 正文：$($paras[0])")
    if ($paras.Count -gt 1) {
      foreach ($p in $paras[1..($paras.Count - 1)]) { [void]$sb.AppendLine(); [void]$sb.AppendLine('  ' + $p) }
    }
  }
  foreach ($a in @($j.ads)) {
    [void]$sb.AppendLine()
    [void]$sb.AppendLine("## $($a.ad_id) $($a.title)")
    $t = $a.targeting
    $parts = @()
    if ($t.domains -and @($t.domains.PSObject.Properties).Count -gt 0) { $parts += '域 ' + (PairStr $t.domains $null) }
    if ($t.sub_tags -and @($t.sub_tags.PSObject.Properties).Count -gt 0) { $parts += '标签 ' + (PairStr $t.sub_tags $null) }
    if ($t.traits -and @($t.traits.PSObject.Properties).Count -gt 0) { $parts += '特质 ' + (PairStr $t.traits $traitShort) }
    [void]$sb.AppendLine('- 模拟广告｜定向 ' + ($parts -join ' / '))
    $adStatus = if ([string]::IsNullOrWhiteSpace([string]$a.review_status)) { 'pending' } else { [string]$a.review_status }
    $adReviewedBy = if ($null -eq $a.reviewed_by) { '' } else { [string]$a.reviewed_by }
    $adReviewedAt = if ($null -eq $a.reviewed_at) { '' } else { [string]$a.reviewed_at }
    [void]$sb.AppendLine("- 审核：$adStatus" + $(if ($adReviewedBy) { "｜审核人 $adReviewedBy" } else { '' }) + $(if ($adReviewedAt) { "｜时间 $adReviewedAt" } else { '' }))
    [void]$sb.AppendLine("- 正文：$($a.body)")
    $marks = @('①','②','③','④','⑤')
    $rs = for ($i = 0; $i -lt @($a.reason_template).Count; $i++) { '{0} "{1}"' -f $marks[$i], @($a.reason_template)[$i] }
    [void]$sb.AppendLine('- 投放理由模板：' + ($rs -join ' '))
  }
  [System.IO.File]::WriteAllText((Join-Path $root $files[$d]), ($sb.ToString() -replace '(?<!\r)\n', "`r`n"), $utf8)
  Write-Host ("{0}: 兴趣 {1} + 探针 {2} + 广告 {3}" -f $files[$d], $nI, $nP, $nA)
}
