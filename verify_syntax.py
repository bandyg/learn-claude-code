import py_compile
import sys
try:
    py_compile.compile('agents/s01_ollama1.py', doraise=True)
    print('Syntax OK')
except py_compile.PyCompileError as e:
    print(f'Syntax Error: {e}')
