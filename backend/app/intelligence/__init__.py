"""
Inteligencia: lo relevante que ocurre FUERA de la empresa, con evidencia.

    fuente oficial (BOE, BDNS, PLACSP)
          ↓  sources/        descarga datos reales (HTTP o archivos importados)
    ExternalItem             común a todos los clientes: es información pública
          ↓  relevance.py    reglas deterministas contra el perfil del cliente
    IntelMatch               por qué le importa a ESTE cliente (✓/✗), relevancia alta/media/baja
          ↓  summarizer.py   la IA resume SOLO candidatos y cada afirmación lleva una cita
                             que se comprueba en el texto oficial; lo que no se puede citar, se descarta
    pantalla Inteligencia    qué ha pasado · de dónde viene · por qué importa · a quién afecta ·
                             qué significa · qué puedo hacer · qué evidencia lo demuestra

La IA nunca es la fuente: interpreta, clasifica y resume. La fuente es el documento oficial.
"""
