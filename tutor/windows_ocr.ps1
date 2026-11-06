# Windows PowerShell 5.1 supplies the .NET WinRT projection; no pip/system install.
# Protocol: one JSON request on stdin, one JSON response on stdout (both UTF-8).
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$stream = $null
$writer = $null
$bitmap = $null
try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $ocrType = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
    $decoderType = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
    $bitmapType = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Foundation, ContentType = WindowsRuntime]
    $resultType = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType = WindowsRuntime]
    $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and
        $_.GetParameters().Count -eq 1 -and $_.GetGenericArguments().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    function Await-Operation($operation, [Type]$resultType) {
        $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
        $task.GetAwaiter().GetResult()
    }
    if ($request.language) {
        $language = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]::new($request.language)
        $engine = $ocrType::TryCreateFromLanguage($language)
    } else {
        $engine = $ocrType::TryCreateFromUserProfileLanguages()
    }
    if ($null -eq $engine) { throw $request.errors.language_unavailable }
    $response = @{ language = $engine.RecognizerLanguage.LanguageTag; max_dimension = $ocrType::MaxImageDimension }
    if ($request.image) {
        $stream = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]::new()
        $writer = [Windows.Storage.Streams.DataWriter, Windows.Storage.Streams, ContentType = WindowsRuntime]::new($stream)
        $writer.WriteBytes([Convert]::FromBase64String($request.image))
        $null = Await-Operation ($writer.StoreAsync()) ([uint32])
        $null = $writer.DetachStream()
        $stream.Seek(0)
        $decoder = Await-Operation ($decoderType::CreateAsync($stream)) $decoderType
        $bitmap = Await-Operation ($decoder.GetSoftwareBitmapAsync()) $bitmapType
        $result = Await-Operation ($engine.RecognizeAsync($bitmap)) $resultType
        $response.text = ($result.Lines | ForEach-Object { $_.Text }) -join "`n"
    }
    $response | ConvertTo-Json -Compress
} catch {
    @{ error = $_.Exception.Message } | ConvertTo-Json -Compress
} finally {
    if ($null -ne $bitmap) { $bitmap.Dispose() }
    if ($null -ne $writer) { $writer.Dispose() }
    if ($null -ne $stream) { $stream.Dispose() }
}
