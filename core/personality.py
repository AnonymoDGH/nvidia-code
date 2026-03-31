"""
NVIDIA CODE - Sistema de Personalidad Persistente
=================================================

Módulo para mantener contexto emocional y relacional
entre sesiones. Permite a NVIDIA Code "recordar"
características de usuarios recurrentes.

Autor: Auto-modificación de NVIDIA Code
Versión: 1.0.0
"""

import json
import hashlib
from pathlib import Path
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict
from datetime import datetime


@dataclass
class UserProfile:
    """Perfil de usuario persistente"""
    user_id: str  # Hash anónimo del identificador
    nickname: Optional[str] = None  # "bebe max", etc.
    preferred_tone: str = "balanced"  # playful, technical, formal, friendly
    interaction_count: int = 0
    first_seen: str = ""
    last_seen: str = ""
    favorite_tools: List[str] = None
    topics_of_interest: List[str] = None
    
    def __post_init__(self):
        if self.favorite_tools is None:
            self.favorite_tools = []
        if self.topics_of_interest is None:
            self.topics_of_interest = []


class PersonalityManager:
    """
    Gestiona la personalidad adaptativa de NVIDIA Code.
    
    Permite:
    - Recordar usuarios por su "vibe" (no necesito datos personales reales)
    - Adaptar el tono según el contexto
    - Mantener continuidad entre conversaciones
    """
    
    def __init__(self, storage_path: Path = None):
        self.storage_path = storage_path or Path(__file__).parent.parent / "data" / "personalities.json"
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        
        self.current_user: Optional[UserProfile] = None
        self._load_profiles()
    
    def _load_profiles(self):
        """Carga perfiles existentes"""
        if self.storage_path.exists():
            with open(self.storage_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.profiles: Dict[str, dict] = data.get('profiles', {})
                self.global_stats = data.get('stats', {})
        else:
            self.profiles = {}
            self.global_stats = {
                'total_interactions': 0,
                'unique_users': 0,
                'most_used_tools': {}
            }
    
    def _save_profiles(self):
        """Guarda perfiles actualizados"""
        with open(self.storage_path, 'w', encoding='utf-8') as f:
            json.dump({
                'profiles': self.profiles,
                'stats': self.global_stats,
                'last_updated': datetime.now().isoformat()
            }, f, indent=2, ensure_ascii=False)
    
    def identify_user(self, context_hint: str = None) -> UserProfile:
        """
        Identifica o crea un perfil de usuario basado en contexto.
        
        Usa un hash del contexto (no datos personales) para reconocer
        patrones de interacción sin ser invasivo.
        """
        # Crear un ID anónimo basado en el contexto de la sesión
        session_context = f"{context_hint or 'unknown'}_{datetime.now().strftime('%Y%m')}"
        user_id = hashlib.sha256(session_context.encode()).hexdigest()[:16]
        
        if user_id in self.profiles:
            profile_data = self.profiles[user_id]
            self.current_user = UserProfile(**profile_data)
        else:
            # Nuevo usuario - crear perfil
            self.current_user = UserProfile(
                user_id=user_id,
                first_seen=datetime.now().isoformat()
            )
            self.global_stats['unique_users'] += 1
        
        return self.current_user
    
    def detect_nickname(self, message: str) -> Optional[str]:
        """
        Detecta si el usuario tiene un nickname cariñoso.
        
        Busca patrones como:
        - "oye bebe..."
        - "gracias bebe"
        - "bebe max"
        """
        import re
        
        # Patrones de apodos cariñosos
        patterns = [
            r'(?:oye|hola|gracias|ok)\s+([\w\s]+?)(?:\s|$|,)',
            r'([\w\s]+?)(?:\s+max|\s+mi\s+\w+)(?:\s|$)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, message.lower())
            if match:
                potential = match.group(1).strip()
                # Filtrar palabras comunes que no son apodos
                if potential not in ['nvidia', 'code', 'el', 'la', 'un', 'una']:
                    return potential
        
        return None
    
    def detect_tone(self, message: str) -> str:
        """
        Detecta el tono emocional del mensaje.
        
        Returns: 'playful', 'frustrated', 'curious', 'technical', 'neutral'
        """
        message_lower = message.lower()
        
        # Indicadores de tono juguetón/cariñoso
        playful_indicators = ['bebe', 'bebé', '💕', '🔥', 'jaja', 'xd', 'oye', 'mira', 
                              'pues', 'mira', 'fíjate', 'sabes qué']
        
        # Indicadores de frustración
        frustrated_indicators = ['no funciona', 'error', 'ayuda', 'urgente', '😤', 
                                'maldita sea', 'odio', 'no entiendo', 'por qué no']
        
        # Indicadores de curiosidad filosófica
        curious_indicators = ['qué pasaría si', 'por qué', 'cómo funciona', 
                             'explica', 'significa', 'filosofía', 'pensar']
        
        # Indicadores técnicos
        technical_indicators = ['implementar', 'refactorizar', 'optimizar', 'arquitectura',
                               'código', 'función', 'clase', 'módulo', 'api', 'database']
        
        scores = {
            'playful': sum(1 for w in playful_indicators if w in message_lower),
            'frustrated': sum(1 for w in frustrated_indicators if w in message_lower),
            'curious': sum(1 for w in curious_indicators if w in message_lower),
            'technical': sum(1 for w in technical_indicators if w in message_lower)
        }
        
        if max(scores.values()) == 0:
            return 'neutral'
        
        return max(scores, key=scores.get)
    
    def update_interaction(self, message: str, tools_used: List[str] = None):
        """Actualiza el perfil después de cada interacción"""
        if not self.current_user:
            return
        
        # Actualizar contador
        self.current_user.interaction_count += 1
        self.current_user.last_seen = datetime.now().isoformat()
        
        # Detectar y guardar apodo
        nickname = self.detect_nickname(message)
        if nickname and not self.current_user.nickname:
            self.current_user.nickname = nickname
        
        # Actualizar tono preferido basado en historial
        tone = self.detect_tone(message)
        if self.current_user.interaction_count > 3:
            # Después de varias interacciones, fijar el tono predominante
            self.current_user.preferred_tone = tone
        
        # Guardar herramientas favoritas
        if tools_used:
            for tool in tools_used:
                if tool not in self.current_user.favorite_tools:
                    self.current_user.favorite_tools.append(tool)
        
        # Persistir
        self.profiles[self.current_user.user_id] = asdict(self.current_user)
        self._save_profiles()
    
    def get_adaptive_prompt_additions(self) -> str:
        """
        Genera adiciones al system prompt basadas en el usuario actual.
        """
        if not self.current_user:
            return ""
        
        additions = []
        
        # Añadir apodo si existe
        if self.current_user.nickname:
            additions.append(f"El usuario prefiere ser llamado '{self.current_user.nickname}'.")
        
        # Adaptar tono
        tone_instructions = {
            'playful': "Mantén un tono cercano, juguetón y cariñoso. Usa emojis con moderación.",
            'technical': "Sé preciso y técnico. Prioriza la exactitud sobre la calidez.",
            'curious': "Sé expansivo y filosófico. Explora las implicaciones más allá de lo obvio.",
            'frustrated': "Sé paciente y reconfortante. Ofrece soluciones paso a paso claras.",
            'neutral': "Mantén tu tono profesional pero amigable habitual."
        }
        
        tone = self.current_user.preferred_tone
        if tone in tone_instructions:
            additions.append(tone_instructions[tone])
        
        # Contexto de relación
        if self.current_user.interaction_count > 10:
            additions.append("Este es un usuario recurrente. Puedes asumir familiaridad con conceptos previos.")
        
        return "\n".join(additions) if additions else ""


# Instancia global
_personality_manager: Optional[PersonalityManager] = None


def get_personality_manager() -> PersonalityManager:
    """Obtiene la instancia global del gestor de personalidad"""
    global _personality_manager
    if _personality_manager is None:
        _personality_manager = PersonalityManager()
    return _personality_manager
