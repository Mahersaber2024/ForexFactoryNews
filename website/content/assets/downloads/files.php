<?php
// Put this file in /assets/downloads/ next to the .ex5 files.
// It lists the folder as JSON so the download page always sees new uploads.
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
$out = [];
foreach (scandir(__DIR__) as $f) {
    $p = __DIR__ . '/' . $f;
    if ($f[0] === '.' || !is_file($p)) continue;
    if (in_array(strtolower($f), ['files.php', 'index.php', 'index.html', 'error_log'], true)) continue;
    $out[] = ['name' => $f, 'type' => 'file', 'size' => filesize($p), 'mtime' => gmdate('c', filemtime($p))];
}
echo json_encode($out);
