"""本地模拟远程上下位机。

在没有真实机器的情况下，用假 SSH/SFTP 服务器提供**符合代码路径规则**的日志，
让「环境资源 → 远程日志查询 / 实时监听」全链路可跑：

- ``fleet``   机群拓扑、账号、日志路径与目录布局
- ``loggen``  以当前时刻为锚生成日志树（含轮转与嵌套归档）
- ``shell``   假远端 shell：find/tar/awk/tail 等命令的执行与路径映射
- ``sshd``    基于 paramiko 的 SSH/SFTP 服务端
- ``seed``    把机群写入 TraceLens 的环境资源（Environment/Machine/Relation）
- ``cli``     命令行入口：init / serve / seed / status / selftest / stop
"""
