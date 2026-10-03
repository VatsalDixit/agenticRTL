import sys
sys.path.insert(0, r'C:/Users/Vatsal/dsw4/agentic')
import tools
cmd = sys.argv[1]
r = tools.eda_shell(cmd, timeout=1800)
print(r.text)
