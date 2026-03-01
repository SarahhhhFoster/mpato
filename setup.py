from setuptools import setup, find_packages

setup(
    name="mpato",
    version="0.1.0",
    description="Multi-Protocol Agent Tool Operator — invoke any REST or WebSocket API via declarative definition files",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    packages=find_packages(),
    python_requires=">=3.8",
    install_requires=[
        "pyyaml>=6.0",
        "requests>=2.28",
    ],
    extras_require={
        "wss": ["websocket-client>=1.6"],
        "all": ["websocket-client>=1.6"],
    },
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
)
