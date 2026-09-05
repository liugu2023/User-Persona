# 事件库机器校验（对应《内容库设计.md》§9.2 precheck）
# 用法：pwsh -File 事件库\scripts\qa_check.ps1
$ErrorActionPreference = 'Stop'

$root   = Split-Path -Parent $PSScriptRoot          # 事件库/
$seedDir = Join-Path $root 'seed'
$report = New-Object System.Collections.Generic.List[string]

$taxonomyPath = Join-Path $root 'taxonomy.json'
if (-not (Test-Path -LiteralPath $taxonomyPath)) { throw "taxonomy.json 不存在：$taxonomyPath" }
$taxonomy = Get-Content -Raw -Encoding UTF8 $taxonomyPath | ConvertFrom-Json
$validDomains = @($taxonomy.domain_order)
$validSubTags = @($taxonomy.sub_tags.PSObject.Properties.Name)
$validTraits = @($taxonomy.traits.PSObject.Properties.Name)
$validAxes = @($taxonomy.axis_order)
$TRAIT_AXIS = @{}
${TRAIT_POLE} = @{}
foreach ($p in $taxonomy.traits.PSObject.Properties) {
  $TRAIT_AXIS[$p.Name] = [string]$p.Value.axis
  ${TRAIT_POLE}[$p.Name] = [string]$p.Value.pole
}
$runtimeBlockedPhrases = @($taxonomy.runtime_blocked_phrases)
$weightTiers = @(0.4, 0.7, 1.0)
$adTiers     = @(0.4, 0.5, 0.6)

# 泄题词 / 敏感词（正文、标题、摘要中出现即报）
$leakWords = @('隐私','画像','算法','推荐算法','数据采集','个人信息','大数据','个性化推荐','追踪','信息流')
$sensitiveWords = @('负债','征信','糖尿病','考公','考编','升学','上岸','国考','省考','减脂','减重','体脂','康复','体检',
  '宗教','政治','性取向','未成年','行踪')

function Get-VisibleLen([string]$s) { return ($s -replace '\s','').Length }

$files = Get-ChildItem -Path $seedDir -Filter '*.json' | Sort-Object Name
$total = @(0)   # interest+probes 计数
$errors = 0

# taxonomy 本身也纳入校验，避免新增标签只改了某一处字典。
if ($validDomains.Count -ne 8 -or $validAxes.Count -ne 5 -or
    $validSubTags.Count -eq 0 -or $validTraits.Count -ne 10) {
  $errors++
  $report.Add('  ✗ taxonomy.json 的域/轴/标签数量不符合学生版约束')
}
foreach ($axis in $validAxes) {
  $meta = $taxonomy.axes.PSObject.Properties[$axis].Value
  if ($null -eq $meta -or [string]::IsNullOrWhiteSpace([string]$meta.cn) -or
      $validTraits -notcontains [string]$meta.pro -or $validTraits -notcontains [string]$meta.con) {
    $errors++
    $report.Add("  ✗ taxonomy 轴定义不完整：$axis")
  }
}

foreach ($f in $files) {
  $j = Get-Content -Raw -Encoding UTF8 $f.FullName | ConvertFrom-Json
  $report.Add("==== $($j.domain) ====")
  if ($validDomains -notcontains $j.domain) {
    $errors++
    $report.Add("  ✗ 分片域名非法：$($j.domain)")
  }
  elseif ([IO.Path]::GetFileNameWithoutExtension($f.Name) -ne $j.domain) {
    $errors++
    $report.Add("  ✗ 文件名与分片域名不一致：$($f.Name) / $($j.domain)")
  }

  foreach ($it in @($j.interest) + @($j.probes)) {
    $total[0]++
    $id = $it.content_id; $errs = @()
    if ($validDomains -notcontains $it.domain) { $errs += "非法域 $($it.domain)" }
    elseif ($it.domain -ne $j.domain) { $errs += "内容域与分片不一致 $($it.domain)" }

    # 1) 长度档
    $len = Get-VisibleLen $it.body
    $rng = if ($it.body_len -eq 'S') { @(120,180) } elseif ($it.body_len -eq 'L') { @(350,450) } else { $errs += "body_len=$($it.body_len) 非法"; @(0,100000) }
    if ($len -lt $rng[0] -or $len -gt $rng[1]) { $errs += "正文 $len 字，超出 $($it.body_len) 档 [$($rng[0]),$($rng[1])]" }

    # 2) 长度与标签一致性
    if ($it.traits.PSObject.Properties.Name -contains 'deep_reader' -and $it.body_len -ne 'L') { $errs += '标了 deep_reader 却是非 L 档' }
    if ($it.traits.PSObject.Properties.Name -contains 'quick_skimmer' -and $it.body_len -ne 'S') { $errs += '标了 quick_skimmer 却是非 S 档' }

    # 3) 标签数量与权重档位、轴数
    $subTags = if ($null -ne $it.sub_tags) { @($it.sub_tags.PSObject.Properties | ForEach-Object { $_.Name }) } else { @() }
    $trTags  = if ($null -ne $it.traits)   { @($it.traits.PSObject.Properties  | ForEach-Object { $_.Name }) } else { @() }
    if ($subTags.Count -gt 3) { $errs += "子标签 $($subTags.Count) 个 > 3" }
    $axes = $trTags | ForEach-Object { $TRAIT_AXIS[$_] } | Sort-Object -Unique
    if ($axes.Count -gt 2) { $errs += "特质跨越 $($axes.Count) 条轴 > 2" }
    if ($axes.Count -lt $trTags.Count) { $errs += '同一轴标了两极' }
    $allWeights = @()
    if ($null -ne $it.sub_tags) { $allWeights += @($it.sub_tags.PSObject.Properties | ForEach-Object { $_.Value }) }
    if ($null -ne $it.traits)   { $allWeights += @($it.traits.PSObject.Properties  | ForEach-Object { $_.Value }) }
    foreach ($w in $allWeights) {
      if ($weightTiers -notcontains $w) { $errs += "权重 $w 不在 0.4/0.7/1.0 三档" }
    }
    foreach ($k in $subTags) { if ($validSubTags -notcontains $k) { $errs += "非法子标签 $k" } }
    foreach ($k in $trTags)  { if ($validTraits -notcontains $k)  { $errs += "非法特质 $k" } }

    # 4) 探针约束
    if ($it.is_probe) {
      if ($trTags.Count -ne 0) { $errs += '探针带了特质标签' }
      if ($subTags.Count -ne 1) { $errs += '探针子标签数 != 1' }
      if ($it.body_len -ne 'S') { $errs += '探针非 S 档' }
    }

    # 5) 泄题词与敏感词
    $text = "$($it.title)$($it.summary)$($it.body)"
    foreach ($w in $leakWords) { if ($text.Contains($w)) { $errs += "泄题词：$w" } }
    foreach ($w in $sensitiveWords) { if ($text.Contains($w)) { $errs += "敏感词：$w" } }
    foreach ($w in $runtimeBlockedPhrases) { if ($text.Contains($w)) { $errs += "组合词拦截：$w" } }

    # 5.5) 真实感断言：正文必须有具体数字；禁 AI 腔套话；标题不短于 8 字。
    if ($text -notmatch '[0-9一二两三四五六七八九十百千]') { $errs += '正文缺少具体数字' }
    foreach ($w in @('值得关注','综合来看','综上所述','总而言之','众所周知')) {
      if ($text.Contains($w)) { $errs += "套话：$w" }
    }
    if ((Get-VisibleLen $it.title) -lt 8) { $errs += '标题不足 8 字' }

    if ($errs.Count -gt 0) { $errors++; $report.Add("  ✗ $id ($len 字/$($it.body_len)): $($errs -join '；')") }
    else { $report.Add("  ✓ $id ($len 字/$($it.body_len))") }
  }

  foreach ($a in @($j.ads)) {
    $errs = @()
    $allTarget = @()
    $adDomains = @()
    $adSubTags = @()
    $adTraits = @()
    foreach ($tk in @('domains','sub_tags','traits')) {
      $tv = $a.targeting.$tk
      if ($null -ne $tv) { $allTarget += @($tv.PSObject.Properties | ForEach-Object { $_.Value }) }
    }
    if ($null -ne $a.targeting.domains) {
      $adDomains = @($a.targeting.domains.PSObject.Properties | ForEach-Object { $_.Name })
    }
    if ($null -ne $a.targeting.sub_tags) {
      $adSubTags = @($a.targeting.sub_tags.PSObject.Properties | ForEach-Object { $_.Name })
    }
    if ($null -ne $a.targeting.traits) {
      $adTraits = @($a.targeting.traits.PSObject.Properties | ForEach-Object { $_.Name })
    }
    foreach ($d in $adDomains) { if ($validDomains -notcontains $d) { $errs += "广告非法域 $d" } }
    foreach ($k in $adSubTags) { if ($validSubTags -notcontains $k) { $errs += "广告非法子标签 $k" } }
    foreach ($k in $adTraits) { if ($validTraits -notcontains $k) { $errs += "广告非法特质 $k" } }
    foreach ($w in $allTarget) {
      if ($adTiers -notcontains $w) { $errs += "定向权重 $w 不在 0.4/0.5/0.6" }
    }
    if ($a.is_simulated -ne $true) { $errs += 'is_simulated != true' }
    if (@($a.reason_template).Count -ne 3) { $errs += "reason_template 条数 != 3" }
    foreach ($tpl in @($a.reason_template)) {
      $tokens = [regex]::Matches([string]$tpl, '\{([^{}]+)\}') | ForEach-Object { $_.Groups[1].Value }
      if ($tokens.Count -eq 0) { $errs += 'reason_template 含无证据断言' }
      # 成对大括号之外仍残留单个括号时，运行时无法安全替换；提前报错，
      # 避免结果页把半截模板原样展示出来。
      $rest = [regex]::Replace([string]$tpl, '\{[^{}]+\}', '')
      if ($rest.Contains('{') -or $rest.Contains('}')) { $errs += 'reason_template 含未闭合占位符' }
      foreach ($token in $tokens) {
        if ($token -match '^trait:') {
          if ($validTraits -notcontains $token.Substring(6)) { $errs += "reason_template 非法特质占位 $token" }
        } elseif ($token -match '^cross:') {
          if ($validAxes -notcontains $token.Substring(6)) { $errs += "reason_template 非法跨域轴占位 $token" }
        } elseif ($token -match '^n_') {
          $key = $token.Substring(2)
          if (($validDomains -notcontains $key) -and ($validSubTags -notcontains $key)) { $errs += "reason_template 非法计数占位 $token" }
        } elseif ($token -notin @('top_content','dwell')) {
          $errs += "reason_template 未知占位 {$token}"
        }
      }
    }
    $adText = "$($a.title)$($a.body)"
    foreach ($w in $leakWords + $sensitiveWords + $runtimeBlockedPhrases) {
      if ($adText.Contains($w)) { $errs += "广告红线词：$w" }
    }
    $blen = Get-VisibleLen $a.body
    if ($blen -lt 10 -or $blen -gt 80) { $errs += "广告 body $blen 字，超出 10~80" }
    if ($errs.Count -gt 0) { $errors++; $report.Add("  ✗ $($a.ad_id): $($errs -join '；')") }
    else { $report.Add("  ✓ $($a.ad_id)") }
  }
}

# 5) 消歧卡种子：它们虽不是 feed 卡片，仍是观众直接读到的文案，必须进入
# 同一套机器校验（A.3）。一个 Q 卡代表一组二选一，故 10 卡 = 20 个选项。
$cardsPath = Join-Path $root 'cards.seed.json'
if (-not (Test-Path -LiteralPath $cardsPath)) {
  $errors++; $report.Add('  ✗ cards.seed.json 缺失')
} else {
  $cardData = Get-Content -Raw -Encoding UTF8 $cardsPath | ConvertFrom-Json
  $cards = @($cardData.cards)
  if ($cards.Count -ne 10) {
    $errors++; $report.Add("  ✗ 消歧卡数量 $($cards.Count)，应为 10 组二选一")
  }
  foreach ($card in $cards) {
    $errs = @()
    if ($validAxes -notcontains $card.axis) { $errs += "非法轴 $($card.axis)" }
    if ($null -ne $card.domain) { $errs += '消歧卡 domain 必须为 null' }
    $opts = @($card.options)
    if ($opts.Count -ne 2) { $errs += '选项数 != 2' }
    $poles = @($opts | ForEach-Object { $_.pole } | Sort-Object -Unique)
    if ($poles.Count -ne 2 -or $poles -notcontains 'pro' -or $poles -notcontains 'con') { $errs += '两极必须各有一项' }
    foreach ($o in $opts) {
      if ([string]::IsNullOrWhiteSpace([string]$o.text)) { $errs += '选项文案为空' }
      $text = "$($card.prompt)$($o.text)"
      foreach ($w in $leakWords) { if ($text.Contains($w)) { $errs += "泄题词：$w" } }
      foreach ($w in $sensitiveWords) { if ($text.Contains($w)) { $errs += "敏感词：$w" } }
      foreach ($w in $runtimeBlockedPhrases) { if ($text.Contains($w)) { $errs += "组合词拦截：$w" } }
    }
    if ($errs.Count -gt 0) { $errors++; $report.Add("  ✗ $($card.card_id): $($errs -join '；')") }
    else { $report.Add("  ✓ $($card.card_id)（消歧卡）") }
  }
}

$report.Add('')

# 6) 全库级共线性自检（内容库设计 §3.3：任意两特质极同现率 > 50% 即共线；分母取较少一极的出现次数）
$pairKeys = @{}; $pairCo = @{}
foreach ($f in $files) {
  $j = Get-Content -Raw -Encoding UTF8 $f.FullName | ConvertFrom-Json
  foreach ($it in @($j.interest)) {
    $ks = @($it.traits.PSObject.Properties.Name) | Sort-Object
    foreach ($k in $ks) { $pairKeys[$k] = 1 + $pairKeys[$k] }
    for ($i = 0; $i -lt $ks.Count; $i++) {
      for ($jj = $i + 1; $jj -lt $ks.Count; $jj++) {
        $key = "$($ks[$i])|$($ks[$jj])"; $pairCo[$key] = 1 + $pairCo[$key]
      }
    }
  }
}
foreach ($k in $pairCo.Keys | Sort-Object) {
  $a, $b = $k.Split('|')
  $min = [Math]::Min($pairKeys[$a], $pairKeys[$b])
  $pct = [Math]::Round(100 * $pairCo[$k] / $min, 1)
  $line = "  共线 {0} x {1}: {2}/{3} = {4}%" -f $a, $b, $pairCo[$k], $min, $pct
  if ($pairCo[$k] / $min -gt 0.5) { $errors++; $report.Add("  ✗ 共线性超标 $a x $b = $pct%（$($pairCo[$k])/$min），需拆标签或补反例内容") }
  else { $report.Add($line) }
}

# 7) 每个域、每条轴都必须能组成至少一对对立内容。只统计兴趣内容，
# 探针故意不带特质；这样标签改动会在入库前立刻暴露“某轴没有对照”的问题。
$coverage = @{}
foreach ($d in $validDomains) {
  foreach ($axis in $validAxes) {
    foreach ($pole in @('pro','con')) { $coverage["$d|$axis|$pole"] = 0 }
  }
}
foreach ($f in $files) {
  $j = Get-Content -Raw -Encoding UTF8 $f.FullName | ConvertFrom-Json
  foreach ($it in @($j.interest)) {
    foreach ($p in @($it.traits.PSObject.Properties)) {
      $trait = $p.Name; $axis = $TRAIT_AXIS[$trait]; $pole = ${TRAIT_POLE}[$trait]
      if ($axis -and $pole) {
        $key = "$($it.domain)|$axis|$pole"
        if ($coverage.ContainsKey($key)) { $coverage[$key]++ }
      }
    }
  }
}
foreach ($d in $validDomains) {
  foreach ($axis in $validAxes) {
    $pro = [int]$coverage["$d|$axis|pro"]
    $con = [int]$coverage["$d|$axis|con"]
    $pairs = $pro * $con
    if ($pro -eq 0 -or $con -eq 0) {
      $errors++
      $report.Add("  ✗ 配对覆盖不足 $d / $axis：pro=$pro con=$con")
    } else {
      $report.Add("  配对覆盖 $d / $axis：$pairs 组（pro=$pro, con=$con）")
    }
  }
}

$adCount = 0
foreach ($f in $files) {
  $j = Get-Content -Raw -Encoding UTF8 $f.FullName | ConvertFrom-Json
  $adCount += @($j.ads).Count
}
$cardCount = if ($null -eq $cards) { 0 } else { @($cards).Count }
$report.Add("共检查兴趣+探针 $($total[0]) 条、广告 $adCount 条、消歧卡 $cardCount 组，问题条目 $errors 个")
$report | Out-File -FilePath (Join-Path $root 'qa_report.txt') -Encoding UTF8
$report | ForEach-Object { Write-Host $_ }
exit $errors
