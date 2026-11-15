from sabor.etl import Pipeline

pipe = Pipeline(label='Pipe')
pipe.run([
    'purge', 
    'extract', 
    'transform', 
    'load'
])
