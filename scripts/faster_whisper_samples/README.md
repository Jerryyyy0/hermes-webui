# faster-whisper 本地验证音频

将需要比较转写效果的音频文件放在此目录（可建立子目录）。目录中的音频会被 Git 忽略，避免将真实语音、录音或测试素材提交到仓库。

不传音频参数时，比较脚本会递归读取此目录中的所有支持格式：

```bash
python scripts/compare_faster_whisper_models.py --models base,small,medium --language zh
```

脚本默认同时验证 `http://192.168.1.137:38080` 上的 SenseVoiceSmall/FunASR
服务；只验证本地 faster-whisper 时添加 `--skip-funasr`。

默认结果只打印到控制台，不会生成文件。需要保存机器可读报告时再显式添加
`--json-out result.json`。

验证远端 SenseVoiceSmall/FunASR 服务：

```bash
python scripts/compare_faster_whisper_models.py \
  --models small,medium --language zh
```

使用单个文件进行模型比较：

```bash
python scripts/compare_faster_whisper_models.py \
  scripts/faster_whisper_samples/example.webm \
  --models base,small,medium --language zh
```
