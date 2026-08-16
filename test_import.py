import sys
sys.path.insert(0, '/home/ec2-user/qc-python')

try:
    from app.api.v1.endpoints import user_tools
    print("Module imported OK")
except SyntaxError as e:
    print(f"Syntax Error: {e}")
    import traceback
    traceback.print_exc()
except Exception as e:
    print(f"Import Error: {type(e).__name__}: {e}")
    import traceback
    traceback.print_exc()
