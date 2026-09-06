create or replace procedure HELLO_FROM_SNOWPARK(name string)
returns string
language python
runtime_version = '3.11'
packages = ('snowflake-snowpark-python')
imports = ('@DEPLOYMENT_STAGE/SNOWPARK/hello_procedure.py')
handler = 'hello_procedure.hello';
