# 1. 确认干净
git status -sb

# 2. 切回 master
git checkout master

# 3. 拉取上游
git fetch upstream

# 4. 让 master 跟上 upstream/master
git merge --ff-only upstream/master

# 5. 再合并 dev
git merge dev

如果第 4 步 --ff-only 失败，说明你的 master 已经有不同于上游的提交，不要硬来，先看：
git log --oneline --graph --decorate --all -20
git log --oneline upstream/master..master
git log --oneline master..upstream/master