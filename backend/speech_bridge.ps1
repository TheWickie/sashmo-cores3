param(
    [Parameter(Mandatory = $true)][ValidateSet('Recognize', 'Wake', 'Synthesize')][string]$Mode,
    [string]$InputPath,
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech

if ($Mode -eq 'Recognize' -or $Mode -eq 'Wake') {
    $culture = [Globalization.CultureInfo]::GetCultureInfo('en-GB')
    $recognizer = [System.Speech.Recognition.SpeechRecognitionEngine]::new($culture)
    try {
        $choices = [System.Speech.Recognition.Choices]::new()
        if ($Mode -eq 'Wake') {
            $choices.Add([string[]]@('Hello Sashmo'))
        } else {
        $choices.Add([string[]]@(
            'How much Codex quota do I have left',
            'How much quota do I have left',
            'How much Codex allowance do I have left',
            'What is my Codex quota',
            'When does my quota reset',
            'When does Codex reset',
            'What time is it',
            'What is the time',
            'What is today''s date',
            'What date is it',
            'What is your connection status',
            'Are you connected'
        ))
        }
        $builder = [System.Speech.Recognition.GrammarBuilder]::new()
        $builder.Culture = $culture
        $builder.Append($choices)
        $recognizer.LoadGrammar([System.Speech.Recognition.Grammar]::new($builder))
        $recognizer.SetInputToWaveFile($InputPath)
        $result = $recognizer.Recognize()
        if ($null -eq $result) {
            [pscustomobject]@{ text = $null; confidence = 0 } | ConvertTo-Json -Compress
        } else {
            [pscustomobject]@{ text = $result.Text; confidence = $result.Confidence } | ConvertTo-Json -Compress
        }
    } finally {
        $recognizer.Dispose()
    }
} else {
    $text = [IO.File]::ReadAllText($InputPath, [Text.Encoding]::UTF8)
    $synthesizer = [System.Speech.Synthesis.SpeechSynthesizer]::new()
    try {
        $synthesizer.Rate = 2
        $format = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(
            16000,
            [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
            [System.Speech.AudioFormat.AudioChannel]::Mono
        )
        $synthesizer.SetOutputToWaveFile($OutputPath, $format)
        $synthesizer.Speak($text)
    } finally {
        $synthesizer.Dispose()
    }
}
