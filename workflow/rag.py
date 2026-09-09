"""RAG (Retrieval-Augmented Generation) system for project context"""

import os
import json
import hashlib
import pickle
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Tuple, Optional
import subprocess
import re

from workflow.git_utils import get_repo
from workflow.config import load_config


# Simple embedding using TF-IDF and cosine similarity (no external dependencies)
class SimpleEmbedding:
    def __init__(self):
        self.vocabulary = set()
        self.idf = {}
        self.documents = []
        self._tokenized_docs = []
    
    def add_document(self, text: str):
        """Add a document to the corpus"""
        self.documents.append(text)
        words = self._tokenize(text)
        self.vocabulary.update(words)
        self._tokenized_docs.append(words)
    
    def build_vocabulary(self):
        """Build vocabulary and calculate IDF"""
        doc_count = len(self.documents)
        for word in self.vocabulary:
            containing_docs = sum(1 for tokens in self._tokenized_docs if word in tokens)
            self.idf[word] = doc_count / (containing_docs + 1)
    
    def embed(self, text: str) -> List[float]:
        """Create embedding for text"""
        words = self._tokenize(text)
        embedding = []
        
        # Create a simple frequency-based embedding using current vocabulary
        # Ensure we always iterate over vocabulary in the same order
        vocab_list = sorted(self.vocabulary)
        for word in vocab_list:
            tf = words.count(word) / len(words) if words else 0
            tfidf = tf * self.idf.get(word, 1)
            embedding.append(tfidf)
        
        return embedding
    
    def embed_with_vocab(self, text: str, vocab: set, idf_dict: dict) -> List[float]:
        """Create embedding using specific vocabulary and IDF"""
        words = self._tokenize(text)
        embedding = []
        
        for word in sorted(vocab):
            tf = words.count(word) / len(words) if words else 0
            tfidf = tf * idf_dict.get(word, 1)
            embedding.append(tfidf)
        
        return embedding
    
    def _tokenize(self, text: str) -> List[str]:
        """Improved tokenization"""
        # Convert to lowercase and extract words
        # Split on non-alphanumeric but keep common technical terms
        tokens = []
        
        # Extract technical terms (with underscores, hyphens, camelCase)
        technical_terms = re.findall(r'\b[a-zA-Z0-9_-]+\b', text.lower())
        tokens.extend(technical_terms)
        
        # Also extract regular words
        words = re.findall(r'\b[a-z]+\b', text.lower())
        tokens.extend(words)
        
        return tokens


class ContextRAG:
    def __init__(self):
        self.context_dir = Path.home() / ".wf" / "rag_context"
        self.context_dir.mkdir(parents=True, exist_ok=True)
        
        self.chunks_file = self.context_dir / "chunks.json"
        self.embeddings_file = self.context_dir / "embeddings.pkl"
        self.index_file = self.context_dir / "index.json"
        
        self.chunks = []
        self.embeddings = []
        self.embedding_model = SimpleEmbedding()
        self.index = {}
        
        self._load_or_create()
    
    def _load_or_create(self):
        """Load existing RAG data or create new"""
        if self.chunks_file.exists() and self.embeddings_file.exists():
            self._load()
        else:
            self._create_index()
    
    def _load(self):
        """Load existing RAG data"""
        try:
            with open(self.chunks_file, 'r') as f:
                self.chunks = json.load(f)
            
            with open(self.embeddings_file, 'rb') as f:
                self.embeddings = pickle.load(f)
            
            with open(self.index_file, 'r') as f:
                self.index = json.load(f)
            
            # Rebuild embedding model
            for chunk in self.chunks:
                self.embedding_model.add_document(chunk['content'])
            self.embedding_model.build_vocabulary()
            
        except Exception as e:
            print(f"⚠️  Could not load RAG data: {e}")
            self._create_index()
    
    def _create_index(self):
        """Create RAG index from project context"""
        print("🔍 Building RAG index for project context...")
        
        # Generate project context chunks
        self.chunks = self._generate_context_chunks()
        
        # Build embeddings
        print("📊 Creating embeddings...")
        for chunk in self.chunks:
            self.embedding_model.add_document(chunk['content'])
        
        self.embedding_model.build_vocabulary()
        
        # Create embeddings for all chunks
        self.embeddings = []
        for chunk in self.chunks:
            embedding = self.embedding_model.embed(chunk['content'])
            self.embeddings.append(embedding)
        
        # Create metadata index
        self.index = {
            'created': datetime.now().isoformat(),
            'chunk_count': len(self.chunks),
            'categories': list(set(chunk['category'] for chunk in self.chunks))
        }
        
        # Save everything
        self._save()
        print(f"✅ RAG index created with {len(self.chunks)} chunks")
    
    def _save(self):
        """Save RAG data"""
        with open(self.chunks_file, 'w') as f:
            json.dump(self.chunks, f, indent=2)
        
        with open(self.embeddings_file, 'wb') as f:
            pickle.dump(self.embeddings, f)
        
        with open(self.index_file, 'w') as f:
            json.dump(self.index, f, indent=2)
    
    def _generate_context_chunks(self) -> List[Dict]:
        """Generate context chunks from project analysis"""
        chunks = []
        
        try:
            repo = get_repo()
            repo_path = str(repo.git_dir).replace("/.git", "")
        except:
            repo_path = os.getcwd()
        
        # Project structure chunks
        chunks.extend(self._create_structure_chunks(repo_path))
        
        # Key files chunks
        chunks.extend(self._create_key_files_chunks(repo_path))
        
        # Dependencies chunks
        chunks.extend(self._create_dependencies_chunks(repo_path))
        
        # Code patterns chunks
        chunks.extend(self._create_code_patterns_chunks(repo_path))
        
        # Commands chunks
        chunks.extend(self._create_commands_chunks(repo_path))
        
        # Testing chunks
        chunks.extend(self._create_testing_chunks(repo_path))
        
        # Workflow chunks
        chunks.extend(self._create_workflow_chunks())
        
        return chunks
    
    def _create_structure_chunks(self, repo_path: str) -> List[Dict]:
        """Create chunks about project structure"""
        chunks = []
        
        try:
            result = subprocess.run(
                ["find", repo_path, "-type", "f", "-name", "*.py", "-o", "-name", "*.js", "-o", "-name", "*.ts", "-o", "-name", "*.jsx", "-o", "-name", "*.tsx", "-o", "-name", "*.go", "-o", "-name", "*.java", "-o", "-name", "*.rb", "-o", "-name", "*.php", "-o", "-name", "*.cpp", "-o", "-name", "*.c", "-o", "-name", "*.h", "-o", "-name", "*.rs", "-o", "-name", "*.swift", "-o", "-name", "*.kt", "-o", "-name", "*.scala", "-o", "-name", "*.cs", "-o", "-name", "*.dart", "-o", "-name", "*.lua", "-o", "-name", "*.r", "-o", "-name", "*.m", "-o", "-name", "*.sh", "-o", "-name", "Dockerfile", "-o", "-name", "Makefile", "-o", "-name", "*.yml", "-o", "-name", "*.yaml", "-o", "-name", "*.json", "-o", "-name", "*.toml", "-o", "-name", "*.cfg", "-o", "-name", "*.ini", "-o", "-name", "*.md"],
                capture_output=True, text=True, timeout=30
            )
            
            if result.returncode == 0:
                files = result.stdout.strip().split('\n')
                important_files = [f for f in files if not any(ignore in f for ignore in [
                    '/node_modules/', '/.git/', '/venv/', '/env/', '/__pycache__/',
                    '/target/', '/build/', '/dist/', '/.pytest_cache/', '/coverage/',
                    '/.vscode/', '/.idea/', '/.DS_Store'
                ])]
                
                # Create directory structure chunk
                structure_info = f"Project structure for {repo_path}:\n"
                for file_path in important_files[:50]:  # Limit to prevent huge chunks
                    rel_path = file_path.replace(repo_path + '/', '')
                    structure_info += f"- {rel_path}\n"
                
                chunks.append({
                    'id': 'project_structure',
                    'category': 'structure',
                    'title': 'Project Structure',
                    'content': structure_info,
                    'metadata': {'file_count': len(important_files)}
                })
                
                # Create chunks for important directories
                dir_structure = {}
                for file_path in important_files:
                    rel_path = file_path.replace(repo_path + '/', '')
                    parts = rel_path.split('/')
                    if len(parts) > 1:
                        dir_name = parts[0]
                        if dir_name not in dir_structure:
                            dir_structure[dir_name] = []
                        dir_structure[dir_name].append(rel_path)
                
                for dir_name, files in dir_structure.items():
                    if len(files) > 3:  # Only include directories with multiple files
                        dir_content = f"Directory: {dir_name}/\n"
                        for file_path in files[:20]:  # Limit per directory
                            dir_content += f"- {file_path}\n"
                        
                        chunks.append({
                            'id': f'directory_{dir_name}',
                            'category': 'structure',
                            'title': f'Directory: {dir_name}',
                            'content': dir_content,
                            'metadata': {'directory': dir_name, 'file_count': len(files)}
                        })
        
        except Exception as e:
            chunks.append({
                'id': 'structure_error',
                'category': 'structure',
                'title': 'Structure Analysis Error',
                'content': f'Could not analyze project structure: {e}',
                'metadata': {'error': str(e)}
            })
        
        return chunks
    
    def _create_key_files_chunks(self, repo_path: str) -> List[Dict]:
        """Create chunks about key configuration files"""
        chunks = []
        
        key_files = {
            'README.md': 'Project documentation and setup instructions',
            'package.json': 'Node.js dependencies, scripts, and project metadata',
            'requirements.txt': 'Python dependencies list',
            'pyproject.toml': 'Python project configuration and build settings',
            'Cargo.toml': 'Rust project configuration and dependencies',
            'pom.xml': 'Maven Java project configuration',
            'build.gradle': 'Gradle Java project configuration',
            'Gemfile': 'Ruby dependencies and project setup',
            'composer.json': 'PHP dependencies and project configuration',
            'go.mod': 'Go modules and dependencies',
            'Dockerfile': 'Docker container configuration',
            'docker-compose.yml': 'Docker compose multi-container setup',
            'Makefile': 'Build automation and commands',
            '.gitignore': 'Git ignore patterns and exclusions',
            '.env.example': 'Environment variables template and documentation',
            '.eslintrc.js': 'JavaScript linting configuration',
            'tsconfig.json': 'TypeScript configuration',
            'pytest.ini': 'Python testing configuration',
            'jest.config.js': 'JavaScript testing configuration',
        }
        
        for filename, description in key_files.items():
            filepath = os.path.join(repo_path, filename)
            if os.path.exists(filepath):
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        content = f.read()
                    
                    # Limit content size for chunks
                    if len(content) > 2000:
                        content = content[:2000] + "\n... (truncated for brevity)"
                    
                    chunks.append({
                        'id': f'key_file_{filename.replace(".", "_")}',
                        'category': 'configuration',
                        'title': f'Key File: {filename}',
                        'content': f"{description}\n\nContent:\n{content}",
                        'metadata': {'filename': filename, 'size': len(content)}
                    })
                except Exception as e:
                    chunks.append({
                        'id': f'key_file_error_{filename.replace(".", "_")}',
                        'category': 'configuration',
                        'title': f'Key File Error: {filename}',
                        'content': f'Could not read {filename}: {e}',
                        'metadata': {'filename': filename, 'error': str(e)}
                    })
        
        return chunks
    
    def _create_dependencies_chunks(self, repo_path: str) -> List[Dict]:
        """Create chunks about project dependencies"""
        chunks = []
        
        # Python dependencies
        req_file = os.path.join(repo_path, "requirements.txt")
        if os.path.exists(req_file):
            try:
                with open(req_file, 'r') as f:
                    deps = [line.strip() for line in f if line.strip() and not line.startswith('#')]
                
                if deps:
                    content = "Python Dependencies:\n" + "\n".join(f"- {dep}" for dep in deps)
                    chunks.append({
                        'id': 'python_dependencies',
                        'category': 'dependencies',
                        'title': 'Python Dependencies',
                        'content': content,
                        'metadata': {'dependency_count': len(deps), 'type': 'python'}
                    })
            except Exception as e:
                chunks.append({
                    'id': 'python_deps_error',
                    'category': 'dependencies',
                    'title': 'Python Dependencies Error',
                    'content': f'Could not read requirements.txt: {e}',
                    'metadata': {'error': str(e)}
                })
        
        # Node.js dependencies
        package_file = os.path.join(repo_path, "package.json")
        if os.path.exists(package_file):
            try:
                with open(package_file, 'r') as f:
                    package_data = json.load(f)
                
                deps = package_data.get("dependencies", {})
                dev_deps = package_data.get("devDependencies", {})
                
                if deps or dev_deps:
                    content = "Node.js Dependencies:\n"
                    
                    if deps:
                        content += "\nProduction Dependencies:\n"
                        for dep, version in list(deps.items())[:20]:
                            content += f"- {dep}: {version}\n"
                    
                    if dev_deps:
                        content += "\nDevelopment Dependencies:\n"
                        for dep, version in list(dev_deps.items())[:10]:
                            content += f"- {dep}: {version}\n"
                    
                    chunks.append({
                        'id': 'nodejs_dependencies',
                        'category': 'dependencies',
                        'title': 'Node.js Dependencies',
                        'content': content,
                        'metadata': {
                            'dependency_count': len(deps) + len(dev_deps),
                            'type': 'nodejs',
                            'production_count': len(deps),
                            'dev_count': len(dev_deps)
                        }
                    })
            except Exception as e:
                chunks.append({
                    'id': 'nodejs_deps_error',
                    'category': 'dependencies',
                    'title': 'Node.js Dependencies Error',
                    'content': f'Could not read package.json: {e}',
                    'metadata': {'error': str(e)}
                })
        
        return chunks
    
    def _create_code_patterns_chunks(self, repo_path: str) -> List[Dict]:
        """Create chunks about code patterns and architecture"""
        chunks = []
        
        # Analyze Python files
        python_files = []
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in ['.git', 'node_modules', '__pycache__', 'venv', 'env', 'target', 'build', 'dist']]
            
            for file in files:
                if file.endswith('.py'):
                    python_files.append(os.path.join(root, file))
        
        if python_files:
            # Classes and patterns
            classes_info = []
            functions_info = []
            imports_info = []
            
            for py_file in python_files[:30]:  # Limit analysis
                try:
                    with open(py_file, 'r', encoding='utf-8') as f:
                        content = f.read()
                    
                    # Extract classes
                    classes = re.findall(r'^class\s+(\w+)(?:\(([^)]+)\))?:', content, re.MULTILINE)
                    for class_name, inheritance in classes:
                        inheritance_info = f" (inherits from {inheritance})" if inheritance else ""
                        rel_path = py_file.replace(repo_path + '/', '')
                        classes_info.append(f"- {class_name}{inheritance_info} in {rel_path}")
                    
                    # Extract functions
                    functions = re.findall(r'^def\s+(\w+)\(([^)]*)\):', content, re.MULTILINE)
                    for func_name, params in functions:
                        rel_path = py_file.replace(repo_path + '/', '')
                        functions_info.append(f"- {func_name}({params}) in {rel_path}")
                    
                    # Extract imports
                    imports = re.findall(r'^(?:from\s+(\w+)|import\s+(\w+))', content, re.MULTILINE)
                    for from_import, direct_import in imports:
                        import_name = from_import or direct_import
                        rel_path = py_file.replace(repo_path + '/', '')
                        imports_info.append(f"- {import_name} in {rel_path}")
                
                except Exception:
                    continue
            
            # Create chunks
            if classes_info:
                chunks.append({
                    'id': 'python_classes',
                    'category': 'patterns',
                    'title': 'Python Classes',
                    'content': "Python classes found in the project:\n" + "\n".join(classes_info[:50]),
                    'metadata': {'class_count': len(classes_info), 'language': 'python'}
                })
            
            if functions_info:
                chunks.append({
                    'id': 'python_functions',
                    'category': 'patterns',
                    'title': 'Python Functions',
                    'content': "Python functions found in the project:\n" + "\n".join(functions_info[:50]),
                    'metadata': {'function_count': len(functions_info), 'language': 'python'}
                })
            
            if imports_info:
                # Count unique imports
                unique_imports = list(set(imp.split(' in ')[0] for imp in imports_info))
                chunks.append({
                    'id': 'python_imports',
                    'category': 'patterns',
                    'title': 'Python Imports',
                    'content': "Python imports used in the project:\n" + "\n".join(f"- {imp}" for imp in unique_imports[:30]),
                    'metadata': {'import_count': len(unique_imports), 'language': 'python'}
                })
        
        return chunks
    
    def _create_commands_chunks(self, repo_path: str) -> List[Dict]:
        """Create chunks about build and development commands"""
        chunks = []
        
        # npm scripts
        package_file = os.path.join(repo_path, "package.json")
        if os.path.exists(package_file):
            try:
                with open(package_file, 'r') as f:
                    package_data = json.load(f)
                
                scripts = package_data.get("scripts", {})
                if scripts:
                    content = "Available npm scripts:\n"
                    for script, command in scripts.items():
                        content += f"- npm run {script}: {command}\n"
                    
                    chunks.append({
                        'id': 'npm_scripts',
                        'category': 'commands',
                        'title': 'NPM Scripts',
                        'content': content,
                        'metadata': {'script_count': len(scripts), 'type': 'npm'}
                    })
            except Exception:
                pass
        
        # Makefile targets
        makefile = os.path.join(repo_path, "Makefile")
        if os.path.exists(makefile):
            try:
                with open(makefile, 'r') as f:
                    makefile_content = f.read()
                
                # Extract targets
                targets = re.findall(r'^(\w+):.*$', makefile_content, re.MULTILINE)
                if targets:
                    content = "Available Makefile targets:\n"
                    for target in targets:
                        content += f"- make {target}\n"
                    
                    chunks.append({
                        'id': 'makefile_targets',
                        'category': 'commands',
                        'title': 'Makefile Targets',
                        'content': content,
                        'metadata': {'target_count': len(targets), 'type': 'makefile'}
                    })
            except Exception:
                pass
        
        # Python common commands
        if os.path.exists(os.path.join(repo_path, "pyproject.toml")) or any(f.endswith('.py') for f in os.listdir(repo_path)):
            content = """Common Python development commands:
- python -m pip install -e . (install in development mode)
- python -m pytest (run tests)
- python -m build (build package)
- python -m pip install -r requirements.txt (install dependencies)
- black . (format code)
- flake8 (lint code)
- mypy (type checking)"""
            
            chunks.append({
                'id': 'python_commands',
                'category': 'commands',
                'title': 'Python Commands',
                'content': content,
                'metadata': {'type': 'python'}
            })
        
        return chunks
    
    def _create_testing_chunks(self, repo_path: str) -> List[Dict]:
        """Create chunks about testing setup"""
        chunks = []
        
        test_info = []
        
        # Look for test directories
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in ['.git', 'node_modules', '__pycache__', 'venv', 'env', 'target', 'build', 'dist']]
            
            if 'test' in dirs or 'tests' in dirs:
                test_dir = os.path.join(root, 'test') if 'test' in dirs else os.path.join(root, 'tests')
                rel_path = test_dir.replace(repo_path + '/', '')
                test_info.append(f"Test directory: {rel_path}")
        
        # Look for test files
        test_files = []
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d not in ['.git', 'node_modules', '__pycache__', 'venv', 'env', 'target', 'build', 'dist']]
            
            for file in files:
                if (file.startswith('test_') or file.endswith('_test.py') or 
                    file.endswith('.test.js') or file.endswith('.spec.js')):
                    rel_path = os.path.join(root, file).replace(repo_path + '/', '')
                    test_files.append(rel_path)
        
        if test_files:
            test_info.append(f"Test files found: {len(test_files)}")
            for test_file in test_files[:10]:
                test_info.append(f"- {test_file}")
        
        # Check for testing frameworks
        frameworks = []
        
        if os.path.exists(os.path.join(repo_path, "pytest.ini")) or any("pytest" in f for f in os.listdir(repo_path) if f.endswith('.py')):
            frameworks.append("pytest")
        
        if os.path.exists(os.path.join(repo_path, "jest.config.js")) or "jest" in os.listdir(repo_path):
            frameworks.append("Jest")
        
        if any("unittest" in f for f in os.listdir(repo_path) if f.endswith('.py')):
            frameworks.append("unittest")
        
        if frameworks:
            test_info.append(f"Testing frameworks: {', '.join(frameworks)}")
        
        if test_info:
            chunks.append({
                'id': 'testing_setup',
                'category': 'testing',
                'title': 'Testing Setup',
                'content': "\n".join(test_info),
                'metadata': {
                    'test_file_count': len(test_files),
                    'frameworks': frameworks,
                    'has_test_dir': len(test_info) > 0 and 'Test directory:' in str(test_info)
                }
            })
        
        return chunks
    
    def _create_workflow_chunks(self) -> List[Dict]:
        """Create chunks about workflow configuration"""
        chunks = []
        
        try:
            cfg = load_config()
            
            content = f"""Workflow Configuration:
Backend: {cfg.get('task_backend', 'Unknown')}
Git enabled: {cfg.get('git_enabled', False)}
GitHub enabled: {cfg.get('github_enabled', False)}"""
            
            if 'jira' in cfg:
                jira_cfg = cfg['jira']
                content += f"""
Jira URL: {jira_cfg.get('url', 'Unknown')}
Jira Project: {jira_cfg.get('project', 'Unknown')}"""

            if 'linear' in cfg:
                # No api_key -- this content gets indexed and fed to the LLM.
                linear_cfg = cfg['linear']
                content += f"""
Linear Team: {linear_cfg.get('team', 'Unknown')}
Linear Workspace: {linear_cfg.get('workspace', 'Unknown')}"""
            
            if 'repositories' in cfg and cfg['repositories']:
                content += f"\nConfigured repositories: {len(cfg['repositories'])}"
                for repo_path, repo_config in cfg['repositories'].items():
                    base_branch = repo_config.get('base_branch', 'default')
                    content += f"\n- {repo_path} (base: {base_branch})"
            
            chunks.append({
                'id': 'workflow_config',
                'category': 'workflow',
                'title': 'Workflow Configuration',
                'content': content,
                'metadata': {
                    'backend': cfg.get('task_backend'),
                    'git_enabled': cfg.get('git_enabled'),
                    'github_enabled': cfg.get('github_enabled'),
                    'repo_count': len(cfg.get('repositories', {}))
                }
            })
        
        except Exception as e:
            chunks.append({
                'id': 'workflow_error',
                'category': 'workflow',
                'title': 'Workflow Configuration Error',
                'content': f'Could not load workflow configuration: {e}',
                'metadata': {'error': str(e)}
            })
        
        return chunks
    
    def retrieve_context(self, query: str, max_chunks: int = 5) -> List[Dict]:
        """Retrieve relevant context chunks for a query"""
        if not self.chunks or not self.embeddings:
            return []
        
        # Create embedding for query using the same method as chunks
        query_embedding = self.embedding_model.embed(query)
        
        # Calculate similarity scores
        similarities = []
        for i, chunk in enumerate(self.chunks):
            # First check for direct substring matching for high relevance
            chunk_content_lower = chunk['content'].lower()
            query_lower = query.lower()
            
            substring_score = 0
            if query_lower in chunk_content_lower:
                # Higher score for exact matches
                substring_score = 0.5
                # Even higher for title matches
                if query_lower in chunk['title'].lower():
                    substring_score = 0.8
            
            # Then calculate TF-IDF similarity
            if i < len(self.embeddings):
                chunk_embedding = self.embeddings[i]
                tfidf_similarity = self._cosine_similarity(query_embedding, chunk_embedding)
            else:
                tfidf_similarity = 0
            
            # Combine scores: substring matching + TF-IDF
            combined_score = max(substring_score, tfidf_similarity)
            similarities.append((i, combined_score, chunk))
            
            # Combine scores: substring matching + TF-IDF
            combined_score = max(substring_score, tfidf_similarity)
            similarities.append((i, combined_score, chunk))
        
        # Sort by similarity and return top chunks
        similarities.sort(key=lambda x: x[1], reverse=True)
        
        relevant_chunks = []
        for i, similarity, chunk in similarities[:max_chunks]:
            if similarity > 0.01:  # Lower threshold for better retrieval
                chunk_copy = chunk.copy()
                chunk_copy['similarity_score'] = similarity
                relevant_chunks.append(chunk_copy)
        
        # If no chunks found above threshold, return top chunk anyway
        if not relevant_chunks and similarities:
            top_similarity = similarities[0][1]
            if top_similarity > 0:  # Return at least one if it has any similarity
                i, _, chunk = similarities[0]
                chunk_copy = chunk.copy()
                chunk_copy['similarity_score'] = top_similarity
                relevant_chunks.append(chunk_copy)
        
        return relevant_chunks
    
    def _cosine_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """Calculate cosine similarity between two vectors"""
        if len(vec1) != len(vec2):
            return 0.0
        
        dot_product = sum(a * b for a, b in zip(vec1, vec2))
        magnitude1 = sum(a * a for a in vec1) ** 0.5
        magnitude2 = sum(b * b for b in vec2) ** 0.5
        
        if magnitude1 == 0 or magnitude2 == 0:
            return 0.0
        
        return dot_product / (magnitude1 * magnitude2)
    
    def get_chunk_by_id(self, chunk_id: str) -> Optional[Dict]:
        """Get a specific chunk by ID"""
        for chunk in self.chunks:
            if chunk['id'] == chunk_id:
                return chunk
        return None
    
    def get_chunks_by_category(self, category: str) -> List[Dict]:
        """Get all chunks in a specific category"""
        return [chunk for chunk in self.chunks if chunk['category'] == category]
    
    def list_categories(self) -> List[str]:
        """List all available categories"""
        return list(set(chunk['category'] for chunk in self.chunks))
    
    def rebuild_index(self):
        """Rebuild the entire RAG index"""
        print("🔄 Rebuilding RAG index...")
        self._create_index()
        print("✅ RAG index rebuilt")


# Get or create fresh RAG instance each time to ensure latest code
def update_rag_with_session(issue_key: str, transcript: str, learnings: str):
    """Update RAG with session learnings"""
    rag = get_rag()
    
    # Create a new chunk for session learnings
    learning_chunk = {
        'id': f'session_learning_{issue_key}_{datetime.now().strftime("%Y%m%d_%H%M%S")}',
        'category': 'session_learnings',
        'title': f'Session Learnings - {issue_key}',
        'content': f"""
Session Learnings for {issue_key}:
Date: {datetime.now().isoformat()}

{learnings}

Session Summary:
{transcript[:500]}{"..." if len(transcript) > 500 else ""}
""",
        'metadata': {
            'issue_key': issue_key,
            'date': datetime.now().isoformat(),
            'type': 'session_learning'
        }
    }
    
    # Add chunk to RAG
    rag.chunks.append(learning_chunk)
    
    # Create embedding for new chunk
    embedding = rag.embedding_model.embed(learning_chunk['content'])
    rag.embeddings.append(embedding)
    
    # Update index
    rag.index['chunk_count'] = len(rag.chunks)
    if 'session_learnings' not in rag.index['categories']:
        rag.index['categories'].append('session_learnings')
    
    # Save updated RAG
    rag._save()


def get_rag() -> ContextRAG:
    """Create new RAG instance to ensure latest improvements"""
    return ContextRAG()