git pull origin main --allow-unrelated-histories
# resolve the trivial README conflict by keeping ours:
git checkout --ours README.md
git add .
git commit -m "Merge remote first-commit"
git push -u origin main